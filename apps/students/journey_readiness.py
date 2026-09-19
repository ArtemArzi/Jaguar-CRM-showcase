"""Aggregate, PII-free rollout readiness for the unified client journey."""

from __future__ import annotations

from collections import Counter
from datetime import datetime

from django.db.models import Q
from django.utils import timezone

from apps.attendance.models import (
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    PersonalServiceTermsSnapshot,
)
from apps.billing.models import (
    BankPaymentOrder,
    Payment,
    Subscription,
    Tariff,
    TariffComponent,
    TrainingType,
)
from apps.billing.service_modules.personal_offers import personal_booking_default_candidates
from apps.clubs.models import Club
from apps.leads.models import LeadLifecycleEvent
from apps.students.models import Student

LIVE_BANK_ORDER_STATUSES = (
    BankPaymentOrder.Status.CREATED,
    BankPaymentOrder.Status.PENDING,
    BankPaymentOrder.Status.AUTHORIZED,
    BankPaymentOrder.Status.MANUAL_REVIEW,
)
LIVE_PERSONAL_RESERVATION_STATUSES = (
    PersonalBookingPaymentReservation.Status.PENDING_PAYMENT,
    PersonalBookingPaymentReservation.Status.MANUAL_REVIEW,
    PersonalBookingPaymentReservation.Status.BOOKED,
)


def audit_unified_client_journey_readiness(*, club_ids: tuple[int, ...] | None = None) -> dict:
    """Return only club IDs, counts, statuses, age metrics, and blocker codes."""

    clubs = Club.objects.order_by("id")
    if club_ids is not None:
        clubs = clubs.filter(id__in=club_ids)

    reports = [_audit_club(club_id=club_id) for club_id in clubs.values_list("id", flat=True)]
    blocker_counts = Counter(
        blocker for report in reports for dimension in report["dimensions"] for blocker in dimension["blocker_codes"]
    )
    invalid_club_count = sum(not report["is_ready"] for report in reports)
    invalid_dimension_count = sum(
        dimension["status"] == "blocked" for report in reports for dimension in report["dimensions"]
    )
    return {
        "is_ready": invalid_club_count == 0,
        "audited_club_count": len(reports),
        "invalid_club_count": invalid_club_count,
        "invalid_dimension_count": invalid_dimension_count,
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "clubs": reports,
    }


def _audit_club(*, club_id: int) -> dict:
    dimensions = [
        _workspace_provenance_dimension(club_id=club_id),
        _early_manual_admission_dimension(club_id=club_id),
        _personal_terms_dimension(club_id=club_id),
        _personal_default_tariff_dimension(club_id=club_id),
        _operational_admission_dimension(club_id=club_id),
        _live_finance_dimension(club_id=club_id),
    ]
    return {
        "club_id": club_id,
        "is_ready": all(dimension["status"] == "ready" for dimension in dimensions),
        "dimensions": dimensions,
    }


def _workspace_provenance_dimension(*, club_id: int) -> dict:
    students = Student.objects.for_club(club_id).filter(deleted_at__isnull=True)
    legacy_without_provenance = students.filter(
        crm_entry_kind=Student.CrmEntryKind.LEGACY_UNKNOWN,
        became_student_at__isnull=True,
    )
    expected_lost_ids = (
        LeadLifecycleEvent.objects.for_club(club_id)
        .filter(
            student_id__in=legacy_without_provenance.filter(status=Student.Status.LOST).values("id"),
            event_type=LeadLifecycleEvent.EventType.LEAD_LOST,
            metadata__student_status_from__in=(Student.Status.LEAD, Student.Status.TRIAL),
        )
        .exclude(
            student_id__in=LeadLifecycleEvent.objects.for_club(club_id)
            .filter(
                event_type=LeadLifecycleEvent.EventType.LEAD_CONVERTED,
            )
            .values("student_id")
        )
        .values("student_id")
    )
    expected_null_count = legacy_without_provenance.filter(
        Q(status__in=(Student.Status.LEAD, Student.Status.TRIAL))
        | Q(status=Student.Status.LOST, id__in=expected_lost_ids)
    ).count()
    ambiguous_count = legacy_without_provenance.count() - expected_null_count
    return _dimension(
        name="workspace_provenance",
        invalid_counts={"ambiguous_legacy_student_provenance": ambiguous_count},
        classification_state="ambiguous_active_like_legacy" if ambiguous_count else "clear",
        live_student_count=students.count(),
        expected_null_legacy_count=expected_null_count,
        ambiguous_legacy_count=ambiguous_count,
    )


def _early_manual_admission_dimension(*, club_id: int) -> dict:
    candidates = Student.objects.for_club(club_id).filter(
        deleted_at__isnull=True,
        crm_entry_kind=Student.CrmEntryKind.LEAD_INTAKE,
        status=Student.Status.ACTIVE,
        became_student_at__isnull=True,
    )
    unsupported_count = 0
    for student_id in candidates.values_list("id", flat=True):
        if not _has_durable_pending_manual_admission(club_id=club_id, student_id=student_id):
            unsupported_count += 1
    return _dimension(
        name="early_manual_admission",
        invalid_counts={"early_manual_admission_without_durable_evidence": unsupported_count},
        classification_state=("manual_admission_reconciliation_required" if unsupported_count else "clear"),
        pending_manual_admission_candidate_count=candidates.count(),
        unsupported_manual_admission_count=unsupported_count,
    )


def _has_durable_pending_manual_admission(*, club_id: int, student_id: int) -> bool:
    payments = (
        Payment.objects.for_club(club_id)
        .filter(
            student_id=student_id,
            deleted_at__isnull=True,
            payment_method__in=(Payment.Method.CASH, Payment.Method.TRANSFER),
            status=Payment.Status.PENDING,
            target_schedule__isnull=False,
            target_start_date__isnull=False,
            subscription__isnull=False,
            subscription__status=Subscription.Status.PENDING,
            subscription__deleted_at__isnull=True,
            conversion_enrollment__isnull=False,
        )
        .select_related("subscription", "conversion_enrollment", "target_schedule", "tariff")
    )
    for payment in payments:
        enrollment = payment.conversion_enrollment
        subscription = payment.subscription
        if enrollment is None or subscription is None or payment.target_schedule is None:
            continue
        if (
            subscription.club_id == club_id
            and subscription.student_id == student_id
            and subscription.tariff_id == payment.tariff_id
            and enrollment.club_id == club_id
            and enrollment.student_id == student_id
            and enrollment.schedule_id == payment.target_schedule_id
            and enrollment.starts_on == payment.target_start_date
        ):
            return True
    return False


def _personal_terms_dimension(*, club_id: int) -> dict:
    live_legacy_partial_count = (
        PersonalServiceTermsSnapshot.objects.for_club(club_id)
        .filter(
            terms_version=PersonalServiceTermsSnapshot.TermsVersion.LEGACY_PARTIAL,
            booking__state__in=(
                PersonalDropInBooking.State.SCHEDULED,
                PersonalDropInBooking.State.ATTENDED,
            ),
        )
        .count()
        + PersonalServiceTermsSnapshot.objects.for_club(club_id)
        .filter(
            terms_version=PersonalServiceTermsSnapshot.TermsVersion.LEGACY_PARTIAL,
            reservation__status__in=LIVE_PERSONAL_RESERVATION_STATUSES,
        )
        .count()
    )
    missing_count = (
        PersonalDropInBooking.objects.for_club(club_id)
        .filter(
            state__in=(PersonalDropInBooking.State.SCHEDULED, PersonalDropInBooking.State.ATTENDED),
            terms_snapshot__isnull=True,
        )
        .count()
        + PersonalBookingPaymentReservation.objects.for_club(club_id)
        .filter(
            status__in=LIVE_PERSONAL_RESERVATION_STATUSES,
            terms_snapshot__isnull=True,
        )
        .count()
    )
    return _dimension(
        name="personal_terms",
        invalid_counts={
            "legacy_partial_personal_terms": live_legacy_partial_count,
            "missing_personal_terms": missing_count,
        },
        classification_state="personal_terms_incomplete" if (live_legacy_partial_count or missing_count) else "clear",
        legacy_partial_count=live_legacy_partial_count,
        missing_count=missing_count,
    )


def _personal_default_tariff_dimension(*, club_id: int) -> dict:
    personal_types = TrainingType.objects.for_club(club_id).filter(
        is_active=True,
        kind=TrainingType.Kind.PERSONAL,
    )
    location_ids = list(Club.objects.get(id=club_id).locations.values_list("id", flat=True))
    invalid_counts = Counter()
    for training_type in personal_types:
        trainer_ids = list(
            Tariff.objects.for_club(club_id)
            .filter(
                training_type_id=training_type.id,
                is_personal_booking_default=True,
                personal_booking_trainer__isnull=False,
            )
            .order_by("personal_booking_trainer_id")
            .values_list("personal_booking_trainer_id", flat=True)
            .distinct()
        )
        for location_id in location_ids:
            generic_candidates = personal_booking_default_candidates(
                club_id=club_id,
                training_type_id=training_type.id,
                location_id=location_id,
                trainer_id=None,
                lock=False,
            )
            generic_tariff = _add_personal_default_candidate_invalid_count(
                invalid_counts=invalid_counts,
                candidates=generic_candidates,
                training_type=training_type,
            )
            if (
                generic_tariff is not None
                and training_type.drop_in_price is not None
                and training_type.drop_in_price != generic_tariff.price
            ):
                invalid_counts["legacy_drop_in_price_mismatch"] += 1

            generic_candidate_ids = [tariff.id for tariff in generic_candidates]
            for trainer_id in trainer_ids:
                trainer_candidates = personal_booking_default_candidates(
                    club_id=club_id,
                    training_type_id=training_type.id,
                    location_id=location_id,
                    trainer_id=trainer_id,
                    lock=False,
                )
                # A trainer without a default in this scope resolves the
                # already-audited generic fallback. Only a selected override
                # adds a distinct catalog contract to audit.
                if [tariff.id for tariff in trainer_candidates] == generic_candidate_ids:
                    continue
                _add_personal_default_candidate_invalid_count(
                    invalid_counts=invalid_counts,
                    candidates=trainer_candidates,
                    training_type=training_type,
                )

    return _dimension(
        name="personal_default_tariff",
        invalid_counts=invalid_counts,
        classification_state="catalog_policy_required" if _has_invalid_counts(invalid_counts) else "clear",
        missing_default_count=invalid_counts["personal_booking_tariff_not_configured"],
        ambiguous_default_count=invalid_counts["personal_booking_tariff_ambiguous"],
        invalid_default_count=invalid_counts["personal_booking_tariff_invalid"],
        legacy_price_mismatch_count=invalid_counts["legacy_drop_in_price_mismatch"],
    )


def _add_personal_default_candidate_invalid_count(
    *,
    invalid_counts: Counter,
    candidates: list[Tariff],
    training_type: TrainingType,
) -> Tariff | None:
    if not candidates:
        invalid_counts["personal_booking_tariff_not_configured"] += 1
        return None
    if len(candidates) > 1:
        invalid_counts["personal_booking_tariff_ambiguous"] += 1
        return None

    tariff = candidates[0]
    if not _is_valid_personal_default(tariff=tariff, training_type=training_type):
        invalid_counts["personal_booking_tariff_invalid"] += 1
        return None
    return tariff


def _is_valid_personal_default(*, tariff: Tariff, training_type: TrainingType) -> bool:
    components = TariffComponent.objects.for_club(tariff.club_id).filter(
        tariff_id=tariff.id,
        is_active=True,
    )
    component = components.first()
    return bool(
        tariff.is_active
        and tariff.name
        and training_type.name
        and tariff.trainings_limit == 1
        and components.count() == 1
        and component is not None
        and component.name
        and component.training_type_id == training_type.id
        and component.entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS
        and component.credits_total == 1
        and component.scope == tariff.scope
        and component.location_id == tariff.location_id
        and component.paid_amount_basis == tariff.price
        and (tariff.trainer_payout_policy or Tariff.PayoutPolicy.ON_CHECKIN) == Tariff.PayoutPolicy.ON_CHECKIN
        and component.trainer_payout_policy == Tariff.PayoutPolicy.ON_CHECKIN
    )


def _operational_admission_dimension(*, club_id: int) -> dict:
    from apps.billing.management.commands.audit_operational_admissions import _audit_club

    audit = _audit_club(club=Club.objects.get(id=club_id))
    invalid_counts = Counter(audit["invalid_state_counts"])
    legacy_unlinked_count = int(audit["legacy_unlinked_pending_manual_count"])
    classifications = Counter(audit["pending_row_classification_counts"])
    reconciliation_required_count = int(classifications["reconcile"])
    # A legacy row that can only drain is visible to operators but does not
    # masquerade as an unclassified provenance anomaly. The authoritative
    # audit already flags unclassified rows as integrity blockers.
    unclassified_count = int(classifications["unclassified"])
    # The operational audit exposes this separately because it identifies the
    # safe reconciliation queue. For a unified-journey activation it is a
    # blocker: switching views while person reconciliation remains would make
    # a known split lifecycle appear clean.
    if reconciliation_required_count:
        invalid_counts["legacy_pending_manual_reconciliation_required"] += (
            reconciliation_required_count
        )
    return _dimension(
        name="operational_admission",
        invalid_counts=invalid_counts,
        classification_state=(
            "operational_admission_reconciliation_required" if _has_invalid_counts(invalid_counts) else "clear"
        ),
        operational_admission_payment_count=int(audit["operational_admission_payment_count"]),
        deferred_provider_event_count=int(audit["deferred_provider_event_count"]),
        legacy_unlinked_pending_manual_count=legacy_unlinked_count,
        manual_admission_origin_counts=dict(audit["manual_admission_origin_counts"]),
        pending_row_classification_counts=dict(audit["pending_row_classification_counts"]),
        legacy_pending_reconcile_count=reconciliation_required_count,
        legacy_pending_drain_count=int(classifications["legacy_drain"]),
        unclassified_pending_manual_count=unclassified_count,
    )


def _live_finance_dimension(*, club_id: int) -> dict:
    payments = Payment.objects.for_club(club_id).filter(deleted_at__isnull=True)
    orders = list(BankPaymentOrder.objects.for_club(club_id).select_related("payment", "subscription", "student"))
    invalid_counts = Counter()
    _add_bank_order_integrity_invalid_counts(orders=orders, invalid_counts=invalid_counts)
    _add_personal_finance_integrity_invalid_counts(
        club_id=club_id,
        orders=orders,
        invalid_counts=invalid_counts,
    )

    live_orders = [order for order in orders if order.status in LIVE_BANK_ORDER_STATUSES]
    live_intent_counts = Counter(order.payment_intent_key for order in live_orders if order.payment_intent_key)
    invalid_counts["duplicate_live_bank_order_intent"] += sum(
        count for count in live_intent_counts.values() if count > 1
    )
    manual_payment_dates = payments.filter(
        status=Payment.Status.PENDING,
        payment_method__in=(Payment.Method.CASH, Payment.Method.TRANSFER),
    ).values_list("created_at", flat=True)
    online_review_dates = [
        order.created_at
        for order in orders
        if order.status == BankPaymentOrder.Status.MANUAL_REVIEW
        and order.payment.deleted_at is None
        and order.payment.payment_method == Payment.Method.ONLINE
    ]
    return _dimension(
        name="live_finance",
        invalid_counts=invalid_counts,
        classification_state=(
            "financial_cardinality_reconciliation_required" if _has_invalid_counts(invalid_counts) else "clear"
        ),
        payment_status_counts=_status_counts(payments),
        bank_order_status_counts=_status_counts(orders),
        live_bank_order_count=len(live_orders),
        manual_review_queues={
            "manual_payment": _queue_age_metrics(created_at_values=manual_payment_dates),
            "online_order": _queue_age_metrics(created_at_values=online_review_dates),
        },
    )


def _add_bank_order_integrity_invalid_counts(*, orders: list[BankPaymentOrder], invalid_counts: Counter) -> None:
    for order in orders:
        if order.payment.club_id != order.club_id or order.subscription.club_id != order.club_id:
            invalid_counts["bank_order_tenancy_mismatch"] += 1
        if order.payment.student_id != order.student_id or order.subscription.student_id != order.student_id:
            invalid_counts["bank_order_student_mismatch"] += 1
        if order.payment.subscription_id != order.subscription_id:
            invalid_counts["bank_order_subscription_mismatch"] += 1


def _add_personal_finance_integrity_invalid_counts(
    *,
    club_id: int,
    orders: list[BankPaymentOrder],
    invalid_counts: Counter,
) -> None:
    reservations = (
        PersonalBookingPaymentReservation.objects.for_club(club_id)
        .filter(
            status__in=LIVE_PERSONAL_RESERVATION_STATUSES,
        )
        .select_related("payment", "subscription", "bank_payment_order", "enrollment")
    )
    for reservation in reservations:
        if reservation.payment_id is None:
            invalid_counts["live_personal_reservation_missing_payment"] += 1
        elif (
            reservation.payment.club_id != club_id
            or reservation.payment.student_id != reservation.student_id
            or reservation.payment.tariff_id != reservation.tariff_id
        ):
            invalid_counts["live_personal_reservation_payment_mismatch"] += 1
        if reservation.subscription_id is None:
            invalid_counts["live_personal_reservation_missing_subscription"] += 1
        elif (
            reservation.subscription.club_id != club_id
            or reservation.subscription.student_id != reservation.student_id
            or reservation.subscription.tariff_id != reservation.tariff_id
            or (
                reservation.payment_id is not None
                and reservation.payment.subscription_id != reservation.subscription_id
            )
        ):
            invalid_counts["live_personal_reservation_subscription_mismatch"] += 1
        if reservation.bank_payment_order_id is None:
            invalid_counts["live_personal_reservation_missing_bank_order"] += 1
        else:
            order = reservation.bank_payment_order
            if (
                order.club_id != club_id
                or order.student_id != reservation.student_id
                or order.payment_id != reservation.payment_id
                or order.subscription_id != reservation.subscription_id
                or order.personal_booking_reservation_id_snapshot != reservation.id
            ):
                invalid_counts["live_personal_reservation_bank_order_mismatch"] += 1
        if reservation.status == PersonalBookingPaymentReservation.Status.BOOKED and reservation.enrollment_id is None:
            invalid_counts["booked_personal_reservation_missing_enrollment"] += 1

    links = list(PersonalDropInPaymentLink.objects.for_club(club_id).select_related(
        "booking__enrollment",
        "payment",
        "bank_payment_order",
    ))
    links_by_order_id = {
        link.bank_payment_order_id: link
        for link in links
        if link.bank_payment_order_id is not None
    }
    for link in links:
        booking = link.booking
        payment = link.payment
        if payment.club_id != club_id or payment.student_id != booking.enrollment.student_id:
            invalid_counts["personal_drop_in_payment_link_owner_mismatch"] += 1
        if payment.tariff_id != booking.tariff_id:
            invalid_counts["personal_drop_in_payment_link_tariff_mismatch"] += 1
        if payment.payment_method == Payment.Method.ONLINE and link.bank_payment_order_id is None:
            invalid_counts["online_personal_drop_in_link_missing_bank_order"] += 1
        if link.bank_payment_order_id:
            order = link.bank_payment_order
            if (
                order.payment_id != payment.id
                or order.personal_drop_in_booking_id_snapshot != booking.id
            ):
                invalid_counts["personal_drop_in_bank_order_link_mismatch"] += 1

    live_orders = [order for order in orders if order.status in LIVE_BANK_ORDER_STATUSES]
    reservation_origin_ids = {
        order.personal_booking_reservation_id_snapshot
        for order in live_orders
        if order.personal_booking_reservation_id_snapshot is not None
    }
    reservation_origins = {
        reservation.id: reservation
        for reservation in PersonalBookingPaymentReservation.objects.for_club(club_id)
        .filter(id__in=reservation_origin_ids)
        .select_related("bank_payment_order")
    }
    drop_in_origin_ids = {
        order.personal_drop_in_booking_id_snapshot
        for order in live_orders
        if order.personal_drop_in_booking_id_snapshot is not None
    }
    drop_in_origins = {
        booking.id: booking
        for booking in PersonalDropInBooking.objects.for_club(club_id).filter(id__in=drop_in_origin_ids)
    }
    for order in live_orders:
        reservation_id = order.personal_booking_reservation_id_snapshot
        if reservation_id is not None:
            reservation = reservation_origins.get(reservation_id)
            if reservation is None:
                invalid_counts["bank_order_personal_reservation_origin_missing"] += 1
            elif (
                reservation.bank_payment_order_id != order.id
                or reservation.payment_id != order.payment_id
                or reservation.subscription_id != order.subscription_id
                or reservation.student_id != order.student_id
            ):
                invalid_counts["bank_order_personal_reservation_origin_mismatch"] += 1

        booking_id = order.personal_drop_in_booking_id_snapshot
        if booking_id is not None:
            booking = drop_in_origins.get(booking_id)
            if booking is None:
                invalid_counts["bank_order_personal_drop_in_origin_missing"] += 1
                continue
            link = links_by_order_id.get(order.id)
            if (
                link is None
                or link.booking_id != booking.id
                or link.payment_id != order.payment_id
                or booking.enrollment.student_id != order.student_id
            ):
                invalid_counts["bank_order_personal_drop_in_origin_mismatch"] += 1


def _status_counts(rows) -> dict[str, int]:
    return dict(sorted(Counter(row.status for row in rows).items()))


def _queue_age_metrics(*, created_at_values) -> dict:
    now = timezone.now()
    bucket_counts = {
        "lt_1h": 0,
        "1h_to_24h": 0,
        "1d_to_7d": 0,
        "gte_7d": 0,
    }
    max_age_seconds = 0
    count = 0
    for created_at in created_at_values:
        count += 1
        age_seconds = _age_seconds(now=now, created_at=created_at)
        max_age_seconds = max(max_age_seconds, age_seconds)
        if age_seconds < 60 * 60:
            bucket_counts["lt_1h"] += 1
        elif age_seconds < 24 * 60 * 60:
            bucket_counts["1h_to_24h"] += 1
        elif age_seconds < 7 * 24 * 60 * 60:
            bucket_counts["1d_to_7d"] += 1
        else:
            bucket_counts["gte_7d"] += 1
    return {
        "count": count,
        "age_bucket_counts": bucket_counts,
        "max_age_seconds": max_age_seconds if count else None,
    }


def _age_seconds(*, now: datetime, created_at: datetime) -> int:
    return max(0, int((now - created_at).total_seconds()))


def _dimension(*, name: str, invalid_counts, classification_state: str, **metrics) -> dict:
    filtered_invalid_counts = {code: count for code, count in sorted(invalid_counts.items()) if count}
    candidate_count = sum(filtered_invalid_counts.values())
    return {
        "name": name,
        "status": "blocked" if candidate_count else "ready",
        "candidate_count": candidate_count,
        "classification_state": classification_state,
        "blocker_codes": list(filtered_invalid_counts),
        **metrics,
    }


def _has_invalid_counts(invalid_counts) -> bool:
    return any(invalid_counts.values())
