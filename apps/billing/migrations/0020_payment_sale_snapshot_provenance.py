from django.db import migrations, models

SALARY_BASIS = "checkin_salary_snapshot"
LEGACY_PROVENANCE = "legacy_backfill_current_state"


def mark_legacy_current_state_snapshots(apps, schema_editor):
    Payment = apps.get_model("billing", "Payment")
    CheckinCascadeEvent = apps.get_model("attendance", "CheckinCascadeEvent")

    Payment.objects.filter(
        sale_earning_snapshot_recorded=True,
        sale_snapshot_provenance="",
    ).update(sale_snapshot_provenance=LEGACY_PROVENANCE)

    events = CheckinCascadeEvent.objects.filter(effect="salary", expected=True)
    for event in events.iterator():
        payload = event.payload or {}
        if payload.get("calculation_basis") != SALARY_BASIS:
            continue
        if payload.get("snapshot_provenance"):
            continue
        payload["snapshot_provenance"] = LEGACY_PROVENANCE
        event.payload = payload
        event.save(update_fields=["payload"])


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0019_backfill_finance_snapshots"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="sale_snapshot_provenance",
            field=models.CharField(
                blank=True,
                choices=[
                    ("confirm_time", "Confirm time"),
                    ("legacy_backfill_current_state", "Legacy backfill current state"),
                ],
                default="",
                max_length=40,
            ),
        ),
        migrations.RunPython(
            mark_legacy_current_state_snapshots,
            migrations.RunPython.noop,
        ),
    ]
