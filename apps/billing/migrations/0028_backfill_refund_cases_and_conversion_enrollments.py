from decimal import Decimal, InvalidOperation

from django.db import migrations


REFUND_FULL = "full"
REFUND_PARTIAL = "partial"
CASE_DETECTED = "detected"
CASE_RECONCILIATION_REQUIRED = "reconciliation_required"
ORDER_REFUNDED = "refunded"
ORDER_REFUNDED_PARTIALLY = "refunded_partially"
REVIEW_MARK_REFUNDED = "mark_refunded"
REVIEW_MARK_REFUNDED_PARTIALLY = "mark_refunded_partially"


def _positive_decimal(value):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount > 0 else None


def backfill_conversion_enrollments(apps, schema_editor):
    Payment = apps.get_model("billing", "Payment")
    ScheduleEnrollment = apps.get_model("attendance", "ScheduleEnrollment")

    payments = Payment.objects.filter(
        status="confirmed",
        target_schedule_id__isnull=False,
        conversion_enrollment_id__isnull=True,
    ).iterator()
    for payment in payments:
        candidates = list(
            ScheduleEnrollment.objects.filter(
                club_id=payment.club_id,
                student_id=payment.student_id,
                schedule_id=payment.target_schedule_id,
                starts_on=payment.target_start_date,
                created_from="paid_conversion",
            )
            .order_by("id")
            .values_list("id", flat=True)[:2]
        )
        if len(candidates) == 1:
            Payment.objects.filter(id=payment.id).update(
                conversion_enrollment_id=candidates[0],
            )


def backfill_refund_cases(apps, schema_editor):
    BankPaymentOrder = apps.get_model("billing", "BankPaymentOrder")
    ProviderEvent = apps.get_model("billing", "BankPaymentProviderEvent")
    ReviewEvent = apps.get_model("billing", "BankPaymentOrderReviewEvent")
    RefundCase = apps.get_model("billing", "PaymentRefundCase")

    provider_events = ProviderEvent.objects.filter(
        order_id__isnull=False,
        provider_status__in=["REFUNDED", "REFUNDED_PARTIALLY"],
    ).select_related("order").order_by("id")
    for event in provider_events.iterator():
        kind = REFUND_FULL if event.provider_status == "REFUNDED" else REFUND_PARTIAL
        detected_amount = (
            _positive_decimal(event.order.amount_snapshot)
            if kind == REFUND_FULL
            else None
        )
        RefundCase.objects.get_or_create(
            provider_event_id=event.id,
            defaults={
                "club_id": event.club_id,
                "order_id": event.order_id,
                "refund_kind": kind,
                "detected_amount": detected_amount,
                "provider_refunded_at": event.received_at,
                "status": (
                    CASE_DETECTED if detected_amount is not None else CASE_RECONCILIATION_REQUIRED
                ),
            },
        )

    review_events = ReviewEvent.objects.filter(
        resolution__in=[REVIEW_MARK_REFUNDED, REVIEW_MARK_REFUNDED_PARTIALLY],
    ).order_by("id")
    for review in review_events.iterator():
        if RefundCase.objects.filter(legacy_review_event_id=review.id).exists():
            continue
        existing = (
            RefundCase.objects.filter(
                club_id=review.club_id,
                order_id=review.order_id,
                legacy_review_event_id__isnull=True,
            )
            .order_by("-provider_refunded_at", "-id")
            .first()
        )
        if existing is not None:
            existing.legacy_review_event_id = review.id
            existing.save(update_fields=["legacy_review_event_id"])
            continue

        kind = (
            REFUND_FULL
            if review.resolution == REVIEW_MARK_REFUNDED
            else REFUND_PARTIAL
        )
        detected_amount = None
        if kind == REFUND_FULL:
            detected_amount = _positive_decimal(review.order.amount_snapshot)
        else:
            detected_amount = _positive_decimal(
                (review.evidence_metadata or {}).get("refund_amount")
            )
        RefundCase.objects.create(
            club_id=review.club_id,
            order_id=review.order_id,
            legacy_review_event_id=review.id,
            refund_kind=kind,
            detected_amount=detected_amount,
            provider_refunded_at=review.created_at,
            status=(
                CASE_DETECTED
                if detected_amount is not None
                else CASE_RECONCILIATION_REQUIRED
            ),
        )

    legacy_orders = BankPaymentOrder.objects.filter(
        status__in=[ORDER_REFUNDED, ORDER_REFUNDED_PARTIALLY],
        refund_cases__isnull=True,
    ).order_by("id")
    for order in legacy_orders.iterator():
        kind = REFUND_FULL if order.status == ORDER_REFUNDED else REFUND_PARTIAL
        RefundCase.objects.create(
            club_id=order.club_id,
            order_id=order.id,
            refund_kind=kind,
            detected_amount=order.amount_snapshot if kind == REFUND_FULL else None,
            provider_refunded_at=order.updated_at,
            status=(
                CASE_DETECTED
                if kind == REFUND_FULL
                else CASE_RECONCILIATION_REQUIRED
            ),
        )


class Migration(migrations.Migration):
    dependencies = [
        ("billing", "0027_payment_conversion_enrollment_and_more"),
    ]

    operations = [
        migrations.RunPython(
            backfill_conversion_enrollments,
            migrations.RunPython.noop,
        ),
        migrations.RunPython(
            backfill_refund_cases,
            migrations.RunPython.noop,
        ),
    ]
