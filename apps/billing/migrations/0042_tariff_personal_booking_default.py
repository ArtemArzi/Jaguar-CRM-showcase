from django.db import migrations, models


def backfill_provable_personal_defaults(apps, schema_editor):
    """Only mark a legacy default when every mutable catalog fact agrees."""
    Tariff = apps.get_model("billing", "Tariff")
    TariffComponent = apps.get_model("billing", "TariffComponent")

    candidates_by_scope = {}
    tariffs = Tariff.objects.filter(
        is_active=True,
        training_type__is_active=True,
        training_type__kind="personal",
        trainings_limit=1,
        price__gt=0,
        training_type__drop_in_price=models.F("price"),
    ).order_by("id")
    for tariff in tariffs.iterator():
        # The new scoped constraint is deliberately only for designated rows.
        # A malformed legacy non-default must neither make the expand fail nor
        # become a designated default through this data migration.
        if (
            not tariff.name
            or not tariff.training_type.name
            or (tariff.scope == "club" and tariff.location_id is not None)
            or (tariff.scope == "location" and tariff.location_id is None)
        ):
            continue
        components = list(
            TariffComponent.objects.filter(tariff_id=tariff.id, is_active=True).order_by("sort_order", "id")
        )
        if len(components) != 1:
            continue
        component = components[0]
        if (
            not component.name
            or component.training_type_id != tariff.training_type_id
            or component.entitlement_kind != "finite_credits"
            or component.credits_total != 1
            or component.scope != tariff.scope
            or component.location_id != tariff.location_id
            or component.paid_amount_basis != tariff.price
            or component.trainer_payout_policy != "on_checkin"
            or (tariff.trainer_payout_policy or "on_checkin") != "on_checkin"
        ):
            continue
        key = (tariff.club_id, tariff.training_type_id, tariff.scope, tariff.location_id)
        candidates_by_scope.setdefault(key, []).append(tariff.id)

    # No name/price ranking: one provable candidate is the only safe backfill.
    # If any location/club scope for a training type is ambiguous, leave the
    # whole type unconfigured so readiness cannot silently use a club fallback
    # in place of an unresolved location policy.
    ambiguous_training_types = {
        (key[0], key[1])
        for key, tariff_ids in candidates_by_scope.items()
        if len(tariff_ids) != 1
    }
    for key, tariff_ids in candidates_by_scope.items():
        if (key[0], key[1]) not in ambiguous_training_types:
            Tariff.objects.filter(id=tariff_ids[0]).update(
                is_personal_booking_default=True,
            )


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0041_repair_legacy_manual_reconciliation_attempts"),
    ]

    operations = [
        migrations.AddField(
            model_name="tariff",
            name="is_personal_booking_default",
            field=models.BooleanField(default=False),
        ),
        migrations.AddConstraint(
            model_name="tariff",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(is_personal_booking_default=False)
                    | models.Q(scope="location", location__isnull=False)
                    | models.Q(scope="club", location__isnull=True)
                ),
                name="billing_tariff_scope_location_consistent",
            ),
        ),
        migrations.AddConstraint(
            model_name="tariff",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_personal_booking_default=True, scope="club"),
                fields=("club", "training_type"),
                name="uniq_personal_booking_club_default",
            ),
        ),
        migrations.AddConstraint(
            model_name="tariff",
            constraint=models.UniqueConstraint(
                condition=models.Q(is_personal_booking_default=True, scope="location"),
                fields=("club", "training_type", "location"),
                name="uniq_personal_booking_location_default",
            ),
        ),
        migrations.RunPython(backfill_provable_personal_defaults, migrations.RunPython.noop),
    ]
