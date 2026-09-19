from django.db import migrations

SALARY_BASIS = "checkin_salary_snapshot"


def _resolve_rate(
    trainer_rate_model,
    *,
    club_id,
    trainer_id,
    location_id,
    training_type_id,
    fallback_any_location=False,
):
    qs = trainer_rate_model.objects.filter(
        club_id=club_id,
        trainer_id=trainer_id,
        training_type_id=training_type_id,
    )
    if location_id is not None:
        exact = qs.filter(location_id=location_id).first()
        if exact is not None:
            return exact.percent
    if fallback_any_location:
        any_location = qs.first()
        if any_location is not None:
            return any_location.percent
    return None


def backfill_checkin_salary_snapshots(apps, schema_editor):
    Checkin = apps.get_model("attendance", "Checkin")
    CheckinCascadeEvent = apps.get_model("attendance", "CheckinCascadeEvent")
    TrainerRate = apps.get_model("trainers", "TrainerRate")

    events = CheckinCascadeEvent.objects.filter(effect="salary", expected=True)
    for event in events.iterator():
        payload = event.payload or {}
        if payload.get("calculation_basis") == SALARY_BASIS:
            continue

        checkin = (
            Checkin.objects.select_related("subscription__tariff", "training_type")
            .filter(id=event.checkin_id)
            .first()
        )
        if checkin is None:
            continue

        rate = _resolve_rate(
            TrainerRate,
            club_id=event.club_id,
            trainer_id=checkin.trainer_id,
            location_id=checkin.location_id,
            training_type_id=checkin.training_type_id,
        )
        subscription_price = None
        if checkin.subscription_id is not None:
            subscription_price = str(checkin.subscription.tariff.price)

        event.payload = {
            "checkin_id": checkin.id,
            "club_id": event.club_id,
            "trainer_id_snapshot": checkin.trainer_id,
            "training_type_id_snapshot": checkin.training_type_id,
            "training_type_kind_snapshot": checkin.training_type.kind,
            "subscription_price_snapshot": subscription_price,
            "rate_percent_snapshot": str(rate) if rate is not None else None,
            "calculation_basis": SALARY_BASIS,
            "snapshot_provenance": "legacy_backfill_current_state",
        }
        event.save(update_fields=["payload"])


def backfill_group_sale_snapshots(apps, schema_editor):
    Payment = apps.get_model("billing", "Payment")
    TrainerRate = apps.get_model("trainers", "TrainerRate")

    payments = (
        Payment.objects.filter(
            status="confirmed",
            seller_trainer_id__isnull=False,
            sale_earning_snapshot_recorded=False,
        )
        .select_related("subscription__tariff__training_type")
    )
    for payment in payments.iterator():
        subscription = payment.subscription
        if subscription is None:
            continue
        tariff = subscription.tariff
        training_type = tariff.training_type
        if training_type.kind != "group":
            continue

        rate = _resolve_rate(
            TrainerRate,
            club_id=payment.club_id,
            trainer_id=payment.seller_trainer_id,
            location_id=tariff.location_id,
            training_type_id=tariff.training_type_id,
            fallback_any_location=True,
        )
        payment.sale_earning_snapshot_recorded = True
        payment.sale_trainer_id_snapshot = payment.seller_trainer_id
        payment.sale_training_type_id_snapshot = tariff.training_type_id
        payment.sale_training_type_kind_snapshot = training_type.kind
        payment.sale_rate_percent_snapshot = rate
        payment.sale_amount_basis_snapshot = payment.amount
        payment.save(
            update_fields=[
                "sale_earning_snapshot_recorded",
                "sale_trainer_id_snapshot",
                "sale_training_type_id_snapshot",
                "sale_training_type_kind_snapshot",
                "sale_rate_percent_snapshot",
                "sale_amount_basis_snapshot",
            ]
        )


def backfill_finance_snapshots(apps, schema_editor):
    backfill_checkin_salary_snapshots(apps, schema_editor)
    backfill_group_sale_snapshots(apps, schema_editor)


class Migration(migrations.Migration):
    dependencies = [
        ("attendance", "0009_kiosk_activation_hardening"),
        ("billing", "0018_payment_sale_amount_basis_snapshot_and_more"),
        ("trainers", "0008_remove_legacy_rate_columns"),
    ]

    operations = [
        migrations.RunPython(backfill_finance_snapshots, migrations.RunPython.noop),
    ]
