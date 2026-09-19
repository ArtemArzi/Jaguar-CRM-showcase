import django.db.models.deletion
from django.db import migrations, models
from django.db.models import F


def backfill_package_owner_trainer(apps, schema_editor):
    Payment = apps.get_model("billing", "Payment")
    TrainerPackageAllocation = apps.get_model("trainers", "TrainerPackageAllocation")

    payment_allocations = (
        TrainerPackageAllocation.objects.filter(
            payment_id__isnull=False,
            is_active=True,
        )
        .values("payment_id", "owner_trainer_id")
        .order_by("id")
    )
    for allocation in payment_allocations.iterator():
        Payment.objects.filter(
            id=allocation["payment_id"],
            package_owner_trainer_id__isnull=True,
        ).update(package_owner_trainer_id=allocation["owner_trainer_id"])

    subscription_allocations = (
        TrainerPackageAllocation.objects.filter(
            subscription_id__isnull=False,
            is_active=True,
        )
        .values("subscription_id", "owner_trainer_id")
        .order_by("id")
    )
    for allocation in subscription_allocations.iterator():
        Payment.objects.filter(
            subscription_id=allocation["subscription_id"],
            package_owner_trainer_id__isnull=True,
        ).update(package_owner_trainer_id=allocation["owner_trainer_id"])

    Payment.objects.filter(
        package_owner_trainer_id__isnull=True,
        seller_trainer_id__isnull=False,
        subscription_id__isnull=False,
        subscription__tariff__training_type__kind__in=["personal", "mini_group"],
    ).update(package_owner_trainer_id=F("seller_trainer_id"))


class Migration(migrations.Migration):
    dependencies = [
        ("attendance", "0010_schedulebookingevent_and_more"),
        ("billing", "0020_payment_sale_snapshot_provenance"),
        ("trainers", "0009_trainer_package_compensation"),
    ]

    operations = [
        migrations.AddField(
            model_name="payment",
            name="package_owner_trainer",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="package_payments",
                to="trainers.trainer",
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="target_schedule",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="conversion_payments",
                to="attendance.schedule",
            ),
        ),
        migrations.AddField(
            model_name="payment",
            name="target_start_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="target_group_name_snapshot",
            field=models.CharField(blank=True, default="", max_length=100),
        ),
        migrations.AddField(
            model_name="payment",
            name="target_location_id_snapshot",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="target_location_name_snapshot",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AddField(
            model_name="payment",
            name="target_trainer_id_snapshot",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="target_trainer_name_snapshot",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="payment",
            name="target_training_type_id_snapshot",
            field=models.PositiveBigIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="payment",
            name="target_training_type_kind_snapshot",
            field=models.CharField(blank=True, default="", max_length=20),
        ),
        migrations.AddField(
            model_name="payment",
            name="sale_attribution_source",
            field=models.CharField(blank=True, default="", max_length=50),
        ),
        migrations.RunPython(
            backfill_package_owner_trainer,
            migrations.RunPython.noop,
        ),
    ]
