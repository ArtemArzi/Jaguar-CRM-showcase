from decimal import Decimal

from django.db import migrations, models
import django.db.models.deletion


def backfill_personal_service_terms(apps, schema_editor):
    PersonalDropInBooking = apps.get_model("attendance", "PersonalDropInBooking")
    PersonalBookingPaymentReservation = apps.get_model("attendance", "PersonalBookingPaymentReservation")
    PersonalServiceTermsSnapshot = apps.get_model("attendance", "PersonalServiceTermsSnapshot")

    def create_partial(*, booking=None, reservation=None, amount=None):
        target = booking or reservation
        PersonalServiceTermsSnapshot.objects.create(
            club_id=target.club_id,
            booking=booking,
            reservation=reservation,
            terms_version="legacy_partial",
            tariff_id_snapshot=target.tariff_id,
            tariff_name_snapshot=(getattr(target, "tariff_name_snapshot", "") or ""),
            base_amount=amount,
            discount_amount=Decimal("0.00"),
            payable_amount=amount,
        )

    # Existing records do not contain immutable tariff duration/name/scope
    # evidence.  Even matching payment/component snapshots cannot prove every
    # D7 field, so migration must never manufacture a complete contract.
    for booking in PersonalDropInBooking.objects.iterator():
        create_partial(booking=booking, amount=booking.price_snapshot)

    for reservation in PersonalBookingPaymentReservation.objects.iterator():
        create_partial(reservation=reservation, amount=None)


class Migration(migrations.Migration):
    dependencies = [
        ("attendance", "0022_alter_scheduleenrollment_created_from_traininggroup_and_more"),
        ("billing", "0042_tariff_personal_booking_default"),
    ]

    operations = [
        migrations.CreateModel(
            name="PersonalServiceTermsSnapshot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("terms_version", models.CharField(choices=[("complete_v1", "Complete v1"), ("legacy_partial", "Legacy partial")], max_length=20)),
                ("tariff_id_snapshot", models.PositiveBigIntegerField(blank=True, null=True)),
                ("tariff_name_snapshot", models.CharField(blank=True, default="", max_length=200)),
                ("training_type_id_snapshot", models.PositiveBigIntegerField(blank=True, null=True)),
                ("training_type_name_snapshot", models.CharField(blank=True, default="", max_length=100)),
                ("base_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ("discount_amount", models.DecimalField(decimal_places=2, default=0, max_digits=10)),
                ("payable_amount", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ("currency", models.CharField(default="RUB", max_length=3)),
                ("duration_days", models.PositiveIntegerField(blank=True, null=True)),
                ("scope", models.CharField(blank=True, default="", max_length=20)),
                ("location_id_snapshot", models.PositiveBigIntegerField(blank=True, null=True)),
                ("location_name_snapshot", models.CharField(blank=True, default="", max_length=200)),
                ("component_name_snapshot", models.CharField(blank=True, default="", max_length=200)),
                ("component_training_type_id_snapshot", models.PositiveBigIntegerField(blank=True, null=True)),
                ("component_training_type_name_snapshot", models.CharField(blank=True, default="", max_length=100)),
                ("component_entitlement_kind", models.CharField(blank=True, default="", max_length=20)),
                ("component_credits_total", models.PositiveIntegerField(blank=True, null=True)),
                ("component_scope", models.CharField(blank=True, default="", max_length=20)),
                ("component_location_id_snapshot", models.PositiveBigIntegerField(blank=True, null=True)),
                ("component_location_name_snapshot", models.CharField(blank=True, default="", max_length=200)),
                ("tariff_trainer_payout_policy", models.CharField(blank=True, default="", max_length=20)),
                ("component_trainer_payout_policy", models.CharField(blank=True, default="", max_length=20)),
                ("component_paid_amount_basis", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ("component_unit_amount_basis", models.DecimalField(blank=True, decimal_places=2, max_digits=10, null=True)),
                ("booking", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="terms_snapshot", to="attendance.personaldropinbooking")),
                ("club", models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name="%(class)ss", to="clubs.club")),
                ("reservation", models.OneToOneField(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name="terms_snapshot", to="attendance.personalbookingpaymentreservation")),
            ],
        ),
        migrations.AddConstraint(
            model_name="personalservicetermssnapshot",
            constraint=models.CheckConstraint(condition=(models.Q(booking__isnull=False, reservation__isnull=True) | models.Q(booking__isnull=True, reservation__isnull=False)), name="attendance_personal_terms_one_target"),
        ),
        migrations.AddConstraint(
            model_name="personalservicetermssnapshot",
            constraint=models.CheckConstraint(condition=models.Q(currency="RUB"), name="attendance_personal_terms_rub_only"),
        ),
        migrations.AddConstraint(
            model_name="personalservicetermssnapshot",
            constraint=models.CheckConstraint(condition=models.Q(discount_amount=0), name="attendance_personal_terms_zero_discount"),
        ),
        migrations.AddIndex(
            model_name="personalservicetermssnapshot",
            index=models.Index(fields=["club", "terms_version"], name="att_personal_terms_version_idx"),
        ),
        migrations.RunPython(backfill_personal_service_terms, migrations.RunPython.noop),
    ]
