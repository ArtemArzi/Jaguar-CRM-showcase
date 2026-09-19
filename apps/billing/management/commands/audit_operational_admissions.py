from __future__ import annotations

import json
from collections import Counter

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Count, Q

from apps.attendance.models import (
    PersonalDropInBooking,
    PersonalDropInPaymentLink,
    PersonalServiceTermsSnapshot,
    ScheduleEnrollment,
    TrainingGroupMembership,
    TrainingGroupMembershipEvent,
)
from apps.billing.models import BankPaymentProviderEvent, Debt, DebtSettlementEvent, Payment, Subscription
from apps.clubs.models import Club


def _exact_personal_payment_link(*, payment: Payment) -> PersonalDropInPaymentLink | None:
    """Return exact live or terminal immutable personal evidence, never a broad payment hint."""

    try:
        link = payment.personal_drop_in_payment_link
    except PersonalDropInPaymentLink.DoesNotExist:
        return None

    booking = link.booking
    enrollment = booking.enrollment
    subscription = payment.subscription
    terms = PersonalServiceTermsSnapshot.objects.for_club(payment.club_id).filter(booking_id=booking.id).first()
    if terms is None or subscription is None:
        return None
    has_live_complete_terms = (
        terms.terms_version
        in {
            PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V1,
            PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V2,
        }
        and booking.state
        in {
            PersonalDropInBooking.State.SCHEDULED,
            PersonalDropInBooking.State.ATTENDED,
        }
    )
    has_terminal_legacy_terms = (
        terms.terms_version == PersonalServiceTermsSnapshot.TermsVersion.LEGACY_PARTIAL
        and payment.status == Payment.Status.CONFIRMED
        and booking.state == PersonalDropInBooking.State.NO_SHOW
    )
    if not (has_live_complete_terms or has_terminal_legacy_terms):
        return None
    if not all(
        (
            payment.club_id == link.club_id == booking.club_id == enrollment.club_id,
            payment.student_id == enrollment.student_id,
            subscription.club_id == payment.club_id,
            subscription.student_id == payment.student_id,
            subscription.tariff_id == payment.tariff_id,
            payment.payment_method in {Payment.Method.CASH, Payment.Method.TRANSFER},
            terms.tariff_id_snapshot == payment.tariff_id,
            terms.payable_amount == payment.amount,
        )
    ):
        return None
    return link


def _classify_pending_manual_row(*, payment: Payment, personal_link: PersonalDropInPaymentLink | None) -> str:
    """Classify pending rows without mutating or inferring missing provenance."""

    if payment.status != Payment.Status.PENDING or payment.payment_method not in {
        Payment.Method.CASH,
        Payment.Method.TRANSFER,
    }:
        return ""
    requires_person_reconciliation = (
        payment.student.lead_status is not None
        or (
            payment.student.status == payment.student.Status.ACTIVE
            and payment.student.became_student_at is None
        )
    )
    if personal_link is not None:
        return "reconcile" if requires_person_reconciliation else "canonical"
    if (
        payment.target_training_group_id is not None
        and payment.group_membership_action_snapshot == Payment.GroupMembershipActionSnapshot.RENEWAL
    ):
        # Renewal is a canonical payment context, but never a new admission
        # root or a candidate for person reconciliation.
        return "canonical"
    if (
        payment.target_training_group_id is not None
        and payment.group_membership_action_snapshot == Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
        and payment.conversion_group_membership_id is not None
        and payment.conversion_enrollment_id is not None
    ):
        return "reconcile" if requires_person_reconciliation else "canonical"
    if (
        payment.conversion_enrollment_id is not None
        and payment.conversion_enrollment.created_from == ScheduleEnrollment.CreatedFrom.PAID_CONVERSION
        and payment.subscription_id is not None
    ):
        # A legacy schedule projection lacks the immutable canonical
        # membership/new-admission proof required by the v2 owner. It can
        # finish its old lifecycle, but reconciliation must never guess.
        return "legacy_drain"
    if payment.conversion_enrollment_id is None and payment.target_training_group_id is None:
        return "legacy_drain"
    return "unclassified"


def _audit_club(*, club: Club) -> dict[str, object]:
    """Return an aggregate-only operational-admission audit for one club."""

    payments = list(
        Payment.objects.for_club(club)
        .filter(Q(target_schedule__isnull=False) | Q(personal_drop_in_payment_link__isnull=False))
        .filter(
            Q(payment_method__in=[Payment.Method.CASH, Payment.Method.TRANSFER])
            | Q(target_training_group__isnull=False)
        )
        .select_related(
            "subscription",
            "conversion_enrollment",
            "target_schedule",
            "target_training_group",
            "target_group_membership",
            "conversion_group_membership",
            "student",
            "personal_drop_in_payment_link__booking__enrollment",
        )
        .order_by("id")
    )
    payment_statuses = Counter(payment.status for payment in payments)
    personal_payment_ids = {
        payment_id
        for payment_id in PersonalDropInPaymentLink.objects.for_club(club).values_list("payment_id", flat=True)
    }
    canonical_group_payments = [payment for payment in payments if payment.target_training_group_id]
    canonical_group_action_counts = Counter(
        payment.group_membership_action_snapshot or "missing"
        for payment in canonical_group_payments
    )
    subscription_statuses = Counter(
        payment.subscription.status if payment.subscription_id else "missing"
        for payment in payments
    )
    enrollment_statuses = Counter(
        payment.conversion_enrollment.status if payment.conversion_enrollment_id else "missing"
        for payment in payments
    )
    invalid = Counter()
    manual_admission_origins = Counter()
    pending_row_classifications = Counter()
    migration_source_membership_ids: dict[int, int] = {}
    for source_enrollment_id, membership_id in (
        TrainingGroupMembershipEvent.objects.for_club(club)
        .filter(
            action__in=["backfilled", "source_linked"],
            source_enrollment_id__isnull=False,
        )
        .order_by("source_enrollment_id", "id")
        .values_list("source_enrollment_id", "membership_id")
    ):
        existing_membership_id = migration_source_membership_ids.setdefault(
            source_enrollment_id, membership_id
        )
        if existing_membership_id != membership_id:
            invalid["migration_source_membership_conflict"] += 1

    for payment in payments:
        enrollment = payment.conversion_enrollment
        subscription = payment.subscription
        target_schedule = payment.target_schedule
        personal_link = _exact_personal_payment_link(payment=payment)
        is_personal_payment = personal_link is not None
        if is_personal_payment:
            manual_admission_origins["personal"] += 1

        if target_schedule is None and not is_personal_payment:
            invalid["target_schedule_missing"] += 1
        elif target_schedule is not None and target_schedule.club_id != club.id:
            invalid["target_schedule_tenancy_mismatch"] += 1

        is_group_payment = payment.target_training_group_id is not None
        if is_group_payment:
            manual_admission_origins["group"] += 1
        if is_group_payment:
            membership = payment.conversion_group_membership
            action = payment.group_membership_action_snapshot
            online_unconfirmed_new_admission = (
                payment.payment_method == Payment.Method.ONLINE
                and payment.status != Payment.Status.CONFIRMED
                and action == Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
            )
            if target_schedule is None:
                invalid["group_target_schedule_missing"] += 1
            elif target_schedule.training_group_id != payment.target_training_group_id:
                invalid["group_target_schedule_mismatch"] += 1
            if action == Payment.GroupMembershipActionSnapshot.NEW_ADMISSION:
                if membership is None:
                    if not online_unconfirmed_new_admission:
                        invalid["group_payment_owned_membership_missing"] += 1
                elif (
                    payment.target_group_membership_id != membership.id
                    or membership.training_group_id != payment.target_training_group_id
                    or membership.student_id != payment.student_id
                    or membership.authority != TrainingGroupMembership.Authority.PAYMENT_OWNED
                ):
                    invalid["group_payment_ownership_mismatch"] += 1
                if enrollment is None:
                    if not online_unconfirmed_new_admission:
                        invalid["group_payment_anchor_missing"] += 1
                elif membership is None or (
                    enrollment.training_group_membership_id != membership.id
                    or enrollment.schedule_id != payment.target_schedule_id
                    or enrollment.created_from != ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION
                ):
                    invalid["group_projection_anchor_mismatch"] += 1
            elif action == Payment.GroupMembershipActionSnapshot.RENEWAL:
                target_membership = payment.target_group_membership
                if target_membership is None:
                    invalid["group_renewal_target_membership_missing"] += 1
                elif (
                    target_membership.club_id != club.id
                    or target_membership.training_group_id != payment.target_training_group_id
                    or target_membership.student_id != payment.student_id
                ):
                    invalid["group_renewal_target_membership_mismatch"] += 1
                if membership is not None or enrollment is not None:
                    invalid["group_renewal_owns_admission"] += 1
            elif action == "":
                target_membership = payment.target_group_membership
                source_membership_id = migration_source_membership_ids.get(payment.conversion_enrollment_id)
                if (
                    target_membership is None
                    or target_membership.id != source_membership_id
                    or target_membership.training_group_id != payment.target_training_group_id
                    or target_membership.student_id != payment.student_id
                ):
                    invalid["group_migration_target_membership_mismatch"] += 1
                elif membership is not None:
                    if (
                        membership.id != target_membership.id
                        or membership.authority != TrainingGroupMembership.Authority.PAYMENT_OWNED
                    ):
                        invalid["group_migration_payment_ownership_mismatch"] += 1
                elif target_membership.authority != TrainingGroupMembership.Authority.INDEPENDENT:
                    invalid["group_migration_payment_ownership_missing"] += 1
            else:
                invalid["group_payment_action_missing"] += 1

        if enrollment is None:
            if (
                payment.status == Payment.Status.PENDING
                and not is_personal_payment
                and not (
                    is_group_payment
                    and (
                        payment.group_membership_action_snapshot
                        == Payment.GroupMembershipActionSnapshot.RENEWAL
                        or online_unconfirmed_new_admission
                    )
                )
            ):
                invalid["missing_enrollment"] += 1
        else:
            if enrollment.club_id != club.id or enrollment.student_id != payment.student_id:
                invalid["ownership_mismatch"] += 1
            if not is_personal_payment and enrollment.schedule_id != payment.target_schedule_id:
                invalid["target_schedule_mismatch"] += 1
            if (
                not is_personal_payment
                and (payment.target_start_date is None or enrollment.starts_on != payment.target_start_date)
            ):
                invalid["target_start_mismatch"] += 1
            if payment.status == Payment.Status.PENDING:
                if enrollment.status in {
                    enrollment.Status.CANCELLED,
                    enrollment.Status.TRANSFERRED,
                }:
                    invalid["pending_terminal_enrollment"] += 1
                elif enrollment.status != enrollment.Status.ACTIVE:
                    invalid["pending_enrollment_not_ready"] += 1
                if (
                    not is_group_payment
                    and not is_personal_payment
                    and enrollment.created_from != enrollment.CreatedFrom.PAID_CONVERSION
                ):
                    invalid["pending_enrollment_not_paid_conversion"] += 1

        if subscription is None:
            invalid["missing_subscription"] += 1
        elif subscription.club_id != club.id or subscription.student_id != payment.student_id:
            invalid["subscription_ownership_mismatch"] += 1
        elif payment.status == Payment.Status.PENDING:
            if subscription.deleted_at is not None:
                invalid["pending_subscription_deleted"] += 1
            if subscription.status != Subscription.Status.PENDING:
                invalid["pending_subscription_not_pending"] += 1

        classification = _classify_pending_manual_row(
            payment=payment,
            personal_link=personal_link,
        )
        if classification:
            pending_row_classifications[classification] += 1

    for enrollment in (
        ScheduleEnrollment.objects.for_club(club)
        .filter(created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION)
        .annotate(payment_reference_count=Count("conversion_payments"))
    ):
        if enrollment.payment_reference_count == 0:
            invalid["orphan_paid_conversion_enrollment"] += 1
        elif enrollment.payment_reference_count > 1:
            invalid["reused_conversion_enrollment"] += 1

    for membership in (
        TrainingGroupMembership.objects.for_club(club)
        .filter(authority=TrainingGroupMembership.Authority.PAYMENT_OWNED)
        .annotate(payment_reference_count=Count("conversion_payments"))
    ):
        if membership.payment_reference_count == 0:
            invalid["orphan_payment_owned_group_membership"] += 1
        elif membership.payment_reference_count > 1:
            invalid["reused_payment_owned_group_membership"] += 1

    deferred_provider_event_count = BankPaymentProviderEvent.objects.for_club(club).filter(
        processing_status=BankPaymentProviderEvent.ProcessingStatus.DEFERRED
    ).count()
    if deferred_provider_event_count:
        invalid["deferred_provider_event"] += deferred_provider_event_count

    payments_by_id = {payment.id: payment for payment in payments}
    reserved_debts = list(
        Debt.objects.unscoped()
        .filter(
            settlement_payment_id__in=payments_by_id,
            resolved_at__isnull=True,
        )
        .select_related("checkin__schedule")
        .order_by("id")
    )
    debts_by_id = {debt.id: debt for debt in reserved_debts}
    latest_event_by_debt_id: dict[int, DebtSettlementEvent] = {}
    for event in (
        DebtSettlementEvent.objects.unscoped()
        .filter(
            Q(payment_id__in=payments_by_id) | Q(debt_id__in=debts_by_id)
        )
        .select_related("debt", "payment")
        .order_by("debt_id", "-created_at", "-id")
    ):
        debt = event.debt
        if event.club_id != debt.club_id:
            invalid["debt_settlement_event_debt_tenancy_mismatch"] += 1
        if event.club_id != event.payment.club_id:
            invalid["debt_settlement_event_payment_tenancy_mismatch"] += 1
        if event.debt_id in debts_by_id:
            latest_event_by_debt_id.setdefault(event.debt_id, event)

    for debt in reserved_debts:
        payment = payments_by_id[debt.settlement_payment_id]
        if debt.club_id != club.id:
            invalid["foreign_debt_reservation"] += 1
        else:
            if debt.student_id != payment.student_id or debt.checkin.student_id != payment.student_id:
                invalid["debt_owner_mismatch"] += 1
            if debt.required_tariff_id != payment.tariff_id:
                invalid["debt_tariff_mismatch"] += 1
            if (
                payment.target_training_type_id_snapshot is not None
                and debt.checkin.training_type_id != payment.target_training_type_id_snapshot
            ):
                invalid["debt_schedule_training_type_mismatch"] += 1
            if (
                payment.target_location_id_snapshot is not None
                and debt.checkin.location_id != payment.target_location_id_snapshot
            ):
                invalid["debt_schedule_location_mismatch"] += 1
            if (
                payment.target_training_group_id is not None
                and debt.checkin.schedule.training_group_id != payment.target_training_group_id
            ):
                invalid["debt_schedule_group_mismatch"] += 1
        if payment.status != Payment.Status.PENDING:
            invalid["terminal_unresolved_debt_reservation"] += 1

        latest_event = latest_event_by_debt_id.get(debt.id)
        if latest_event is None:
            invalid["debt_reservation_lifecycle_missing"] += 1
        elif (
            latest_event.event_type != DebtSettlementEvent.EventType.RESERVED
            or latest_event.payment_id != debt.settlement_payment_id
        ):
            invalid["debt_reservation_payment_mismatch"] += 1

    pending_admission_keys = Counter(
        (payment.student_id, payment.target_schedule_id, payment.target_start_date)
        for payment in payments
        if payment.status == Payment.Status.PENDING
        and payment.subscription_id is not None
        and payment.subscription.status == Subscription.Status.PENDING
        and payment.id not in personal_payment_ids
    )
    ambiguous_count = sum(count for count in pending_admission_keys.values() if count > 1)
    if ambiguous_count:
        invalid["ambiguous_pending_admission"] = ambiguous_count

    legacy_unlinked_pending_manual = pending_row_classifications["legacy_drain"]
    reconciliation_required_count = pending_row_classifications["reconcile"]
    if pending_row_classifications["unclassified"]:
        invalid["manual_admission_provenance_unclassified"] += pending_row_classifications["unclassified"]
    payment_owned_group_membership_count = TrainingGroupMembership.objects.for_club(club).filter(
        authority=TrainingGroupMembership.Authority.PAYMENT_OWNED
    ).count()
    payment_owned_group_projection_count = ScheduleEnrollment.objects.for_club(club).filter(
        training_group_membership__authority=TrainingGroupMembership.Authority.PAYMENT_OWNED,
        created_from=ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION,
    ).count()
    invalid_counts = dict(sorted(invalid.items()))
    return {
        "club_id": club.id,
        "operational_admission_payment_count": len(payments),
        "payment_status_counts": dict(sorted(payment_statuses.items())),
        "subscription_status_counts": dict(sorted(subscription_statuses.items())),
        "enrollment_status_counts": dict(sorted(enrollment_statuses.items())),
        "canonical_group_payment_count": len(canonical_group_payments),
        "canonical_group_action_counts": dict(sorted(canonical_group_action_counts.items())),
        "payment_owned_group_membership_count": payment_owned_group_membership_count,
        "payment_owned_group_projection_count": payment_owned_group_projection_count,
        "manual_admission_origin_counts": dict(sorted(manual_admission_origins.items())),
        "pending_row_classification_counts": dict(sorted(pending_row_classifications.items())),
        "deferred_provider_event_count": deferred_provider_event_count,
        "invalid_state_counts": invalid_counts,
        "legacy_unlinked_pending_manual_count": legacy_unlinked_pending_manual,
        "reconciliation_required_count": reconciliation_required_count,
        "clean": (
            not invalid_counts
            and not legacy_unlinked_pending_manual
            and not reconciliation_required_count
        ),
    }


class Command(BaseCommand):
    help = "Report aggregate-only operational-admission rollout and rollback state."

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group(required=True)
        scope.add_argument("--club-id", type=int)
        scope.add_argument("--all-clubs", action="store_true")
        parser.add_argument("--fail-on-invalid", action="store_true")

    def handle(self, *args, **options):
        if options["all_clubs"]:
            report = self._audit_all_clubs()
        else:
            club = Club.objects.filter(id=options["club_id"]).first()
            if club is None:
                raise CommandError("club not found")
            report = _audit_club(club=club)

        self.stdout.write(json.dumps(report, sort_keys=True))
        if options["fail_on_invalid"] and not report["clean"]:
            raise CommandError("operational admission audit is not clean")

    def _audit_all_clubs(self) -> dict[str, object]:
        club_reports = []
        aggregate_invalid = Counter()
        legacy_count = 0
        reconciliation_required_count = 0
        audit_error_count = 0
        clubs = list(Club.objects.order_by("id"))

        for club in clubs:
            try:
                report = _audit_club(club=club)
            except Exception:
                audit_error_count += 1
                aggregate_invalid["audit_failure"] += 1
                club_reports.append({"club_id": club.id, "audit_error": True, "clean": False})
                continue

            club_reports.append(report)
            aggregate_invalid.update(report["invalid_state_counts"])
            legacy_count += report["legacy_unlinked_pending_manual_count"]
            reconciliation_required_count += report["reconciliation_required_count"]

        audited_club_count = len(clubs) - audit_error_count
        aggregate_invalid_counts = dict(sorted(aggregate_invalid.items()))
        clean = (
            audited_club_count == len(clubs)
            and not aggregate_invalid_counts
            and legacy_count == 0
            and reconciliation_required_count == 0
        )
        return {
            "scope": "all_clubs",
            "total_club_count": len(clubs),
            "audited_club_count": audited_club_count,
            "club_reports": club_reports,
            "aggregate_invalid_state_counts": aggregate_invalid_counts,
            "legacy_unlinked_pending_manual_count": legacy_count,
            "reconciliation_required_count": reconciliation_required_count,
            "clean": clean,
        }
