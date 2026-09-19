from django.db import migrations, models
from django.db.models import Count


def assert_no_manual_review_slot_conflicts(apps, schema_editor):
    Reservation = apps.get_model("attendance", "PersonalBookingPaymentReservation")
    conflicts = list(
        Reservation.objects.filter(status__in=["pending_payment", "booked", "manual_review"])
        .values("club_id", "trainer_id", "starts_at", "ends_at")
        .annotate(total=Count("id"))
        .filter(total__gt=1)
        .order_by("club_id", "trainer_id", "starts_at")[:5]
    )
    if conflicts:
        raise RuntimeError(
            "Cannot add uniq_active_personal_payment_reservation_slot: "
            "existing manual_review/pending/booked reservations share a trainer slot. "
            f"Resolve duplicate reservation groups first: {conflicts}"
        )


class Migration(migrations.Migration):

    dependencies = [
        ("attendance", "0018_personal_availability_blocked"),
    ]

    operations = [
        migrations.RunPython(
            assert_no_manual_review_slot_conflicts,
            migrations.RunPython.noop,
        ),
        migrations.RemoveConstraint(
            model_name="personalbookingpaymentreservation",
            name="uniq_active_personal_payment_reservation_slot",
        ),
        migrations.AddConstraint(
            model_name="personalbookingpaymentreservation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("status__in", ["pending_payment", "booked", "manual_review"])),
                fields=("club", "trainer", "starts_at", "ends_at"),
                name="uniq_active_personal_payment_reservation_slot",
            ),
        ),
    ]
