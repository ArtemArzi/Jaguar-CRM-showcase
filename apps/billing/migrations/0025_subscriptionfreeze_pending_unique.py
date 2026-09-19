# Generated manually for P2 duplicate pending freeze protection.

from django.db import migrations, models
from django.db.models import Count
from django.utils import timezone


def reject_duplicate_pending_freezes(apps, schema_editor):
    SubscriptionFreeze = apps.get_model("billing", "SubscriptionFreeze")
    duplicate_groups = (
        SubscriptionFreeze.objects.filter(status="pending")
        .values("club_id", "subscription_id")
        .annotate(total=Count("id"))
        .filter(total__gt=1)
    )
    now = timezone.now()
    for group in duplicate_groups:
        pending_ids = list(
            SubscriptionFreeze.objects.filter(
                club_id=group["club_id"],
                subscription_id=group["subscription_id"],
                status="pending",
            )
            .order_by("created_at", "id")
            .values_list("id", flat=True)
        )
        ids_to_reject = pending_ids[1:]
        if not ids_to_reject:
            continue
        SubscriptionFreeze.objects.filter(id__in=ids_to_reject).update(
            status="rejected",
            approved_by_id=None,
            rejected_by_id=None,
            decision_at=now,
            decision_reason="Закрыта автоматически перед ограничением дублей pending-заявок",
            updated_at=now,
        )


class Migration(migrations.Migration):

    dependencies = [
        ("billing", "0024_bankpaymentorderreviewevent"),
    ]

    operations = [
        migrations.RunPython(reject_duplicate_pending_freezes, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="subscriptionfreeze",
            constraint=models.UniqueConstraint(
                fields=("club", "subscription"),
                condition=models.Q(status="pending"),
                name="uniq_pending_subscription_freeze_per_sub",
            ),
        ),
    ]
