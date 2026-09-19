from __future__ import annotations

import logging
from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

import apps.billing.service_modules.group_payments as group_payments
import apps.billing.service_modules.sale_earnings as sale_earnings
from apps.billing.models import (
    Debt,
    DebtLifecycleEvent,
    DebtSettlementEvent,
    Payment,
    Subscription,
    SubscriptionComponent,
    Tariff,
    TrainingType,
)
from apps.billing.service_modules.debts import (
    _attach_debts_to_subscription,
    assert_payment_reservation_capacity,
    debt_state,
    record_debt_lifecycle_event,
    record_debt_settlement_events,
)
from apps.billing.service_modules.entitlements import (
    _create_subscription_components,
    _resolve_package_owner_trainer_id_for_components,
)
from apps.clubs.timezones import club_local_day_start_by_id, club_localdate_by_id
from apps.common.exceptions import BusinessLogicError

logger = logging.getLogger(__name__)


def _personal_terms_duration_days(*, club_id: int, payment_id: int) -> int | None:
    """Return frozen duration for a complete personal financial family."""

    from apps.attendance.models import PersonalServiceTermsSnapshot, complete_personal_terms_queryset

    terms = (
        complete_personal_terms_queryset(
            PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(
            models.Q(booking__payment_links__payment_id=payment_id)
            | models.Q(reservation__payment_id=payment_id),
        )
        )
        .values_list("duration_days", flat=True)
        .first()
    )
    return terms


def verify_payment(
    *,
    payment_id: int,
    club_id: int,
    verified_by_id: int | None,
    action: str,
    rejection_reason: str = "",
    verified_at=None,
    allow_online: bool = False,
) -> Payment:
    if action not in {"confirm", "reject"}:
        raise BusinessLogicError(
            "Некорректное действие проверки оплаты",
            code="invalid_payment_verify_action",
        )
    if action == "reject":
        rejection_reason = rejection_reason.strip()
        if not rejection_reason:
            raise BusinessLogicError(
                "Укажите причину отклонения оплаты",
                code="payment_rejection_reason_required",
            )

    salary_checkin_ids: list[int] = []
    effective_verified_at = verified_at or timezone.now()
    if timezone.is_naive(effective_verified_at):
        effective_verified_at = timezone.make_aware(
            effective_verified_at,
            timezone.get_current_timezone(),
        )
    with transaction.atomic():
        from apps.attendance.services.training_group_memberships import lock_training_group_mutation_scope

        # Reconciliation may attach canonical ownership to a currently legacy
        # payment. Confirm/reject therefore must quiesce before the payment
        # row is locked, not only after it already has a canonical FK.
        rollout_state = lock_training_group_mutation_scope(club_id=club_id)
        from apps.attendance.services.personal_locking import lock_complete_personal_scopes

        ordered_scope = lock_complete_personal_scopes(club_id=club_id, payment_ids=[payment_id])
        payment_queryset = Payment.objects.for_club(club_id).select_related(
            "package_owner_trainer",
            "subscription__tariff__training_type",
            "target_schedule",
        )
        if ordered_scope is None:
            payment = payment_queryset.select_for_update(of=("self",)).get(id=payment_id)
        else:
            # The ordered helper already owns the row lock; reload relations
            # without introducing a late lock on an earlier personal owner.
            payment = payment_queryset.get(id=payment_id)

        # Idempotent: already processed
        if payment.status != Payment.Status.PENDING:
            return payment

        if payment.payment_method == Payment.Method.ONLINE and not allow_online:
            raise BusinessLogicError(
                "Онлайн-оплаты обрабатываются через ссылку на оплату",
                code="online_payment_manual_verification_forbidden",
            )

        if action == "confirm":
            target_schedule = None
            if payment.subscription:
                target_schedule = group_payments._validate_payment_conversion_target_for_confirm(
                    payment=payment,
                    subscription=payment.subscription,
                    club_id=club_id,
                )
            if (
                payment.group_membership_action_snapshot
                == Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
                and payment.conversion_group_membership_id is None
            ):
                group_payments._link_payment_owned_group_membership(
                    payment=payment,
                    club_id=club_id,
                    actor_user_id=verified_by_id,
                    scope_locked=rollout_state is not None,
                )
            group_payments._lock_and_validate_payment_conversion_enrollment_for_confirm(
                payment=payment,
                club_id=club_id,
            )
            package_owner_trainer_id = None
            subscription_components: list[SubscriptionComponent] = []
            if payment.subscription:
                subscription_components = list(
                    SubscriptionComponent.objects.for_club(club_id)
                    .filter(subscription=payment.subscription, is_active=True)
                    .select_related("training_type", "location")
                    .order_by("id")
                )
                if not subscription_components:
                    subscription_components = _create_subscription_components(
                        subscription=payment.subscription,
                        club_id=club_id,
                        paid_amount=payment.amount,
                    )
                package_owner_trainer_id = _resolve_package_owner_trainer_id_for_components(
                    components=subscription_components,
                    seller_trainer_id=payment.seller_trainer_id,
                    package_owner_trainer_id=payment.package_owner_trainer_id,
                )
                # Re-run the same deterministic allocator used at reservation
                # time while all payment-owned debts and components are locked.
                # A delayed confirmation must not make an over-capacity pending
                # admission financially active.
                assert_payment_reservation_capacity(
                    payment=payment,
                    subscription=payment.subscription,
                    club_id=club_id,
                )

            if (
                payment.subscription
                and any(
                    component.trainer_payout_policy_snapshot == Tariff.PayoutPolicy.ON_PAYMENT
                    for component in subscription_components
                )
            ):
                from apps.trainers.services import lock_and_assert_trainer_payroll_date_open

                lock_and_assert_trainer_payroll_date_open(
                    club_id=club_id,
                    target_date=club_localdate_by_id(club_id, effective_verified_at),
                )

            payment.status = Payment.Status.CONFIRMED
            payment.verified_by_id = verified_by_id
            payment.verified_at = effective_verified_at
            payment.save(update_fields=["status", "verified_by_id", "verified_at", "updated_at"])

            if payment.subscription:
                sub = payment.subscription
                if payment.conversion_enrollment_id:
                    if payment.target_start_date is None:
                        raise BusinessLogicError(
                            "У операционной записи не указана дата старта",
                            code="payment_conversion_target_required",
                        )
                    sub.expires_at = club_local_day_start_by_id(
                        club_id,
                        payment.target_start_date + timedelta(days=sub.tariff.duration_days),
                    )
                    sub.status = (
                        Subscription.Status.ACTIVE
                        if sub.expires_at > effective_verified_at
                        else Subscription.Status.EXPIRED
                    )
                else:
                    sub.status = Subscription.Status.ACTIVE
                    duration_days = _personal_terms_duration_days(
                        club_id=club_id,
                        payment_id=payment.id,
                    ) or sub.tariff.duration_days
                    sub.expires_at = effective_verified_at + timedelta(days=duration_days)
                update_fields = ["status", "expires_at", "updated_at"]
                if sub.paid_amount is None:
                    sub.paid_amount = payment.amount
                    update_fields.append("paid_amount")
                sub.save(update_fields=update_fields)

                # Renewal source validation and compatible carry happen before
                # this outer transaction commits the confirmed payment.  A
                # source that closed early therefore cannot leave a partially
                # confirmed manual or provider family behind.
                from apps.billing.service_modules.renewals import finalize_subscription_renewal

                finalize_subscription_renewal(
                    club_id=club_id,
                    payment_id=payment.id,
                    finalized_at=effective_verified_at,
                )

                explicit_debt_ids = list(
                    Debt.objects.for_club(club_id)
                    .filter(settlement_payment_id=payment.id, resolved_at__isnull=True)
                    .values_list("id", flat=True)
                )
                if explicit_debt_ids:
                    salary_checkin_ids.extend(
                        _attach_debts_to_subscription(
                            subscription=sub,
                            club_id=club_id,
                            student_id=payment.student_id,
                            resolution_type="payment",
                            debt_ids=explicit_debt_ids,
                            settlement_payment_id=payment.id,
                            require_existing_reservation=True,
                            actor_user_id=verified_by_id,
                            lifecycle_event_type=DebtLifecycleEvent.EventType.CONFIRMED,
                        )
                    )
                    confirmed_debt_ids = list(
                        Debt.objects.for_club(club_id)
                        .filter(
                            id__in=explicit_debt_ids,
                            settlement_payment_id=payment.id,
                            resolved_at__isnull=False,
                        )
                        .values_list("id", flat=True)
                    )
                    record_debt_settlement_events(
                        club_id=club_id,
                        payment=payment,
                        debt_ids=confirmed_debt_ids,
                        event_type=DebtSettlementEvent.EventType.CONFIRMED,
                    )
                from apps.trainers.services import create_package_allocation_for_subscription

                create_package_allocation_for_subscription(
                    club_id=club_id,
                    subscription_id=sub.id,
                    owner_trainer_id=package_owner_trainer_id,
                    payment_id=payment.id,
                    created_by_id=verified_by_id,
                    source="payment",
                )

                # A canonical group renewal is tied to the already-owned group
                # membership and exact subscription source.  It is not a new
                # operational admission, so it must not require or create a
                # conversion enrollment on manual confirmation.
                if (
                    target_schedule is not None
                    and payment.conversion_enrollment_id is None
                    and payment.group_membership_action_snapshot
                    != Payment.GroupMembershipActionSnapshot.RENEWAL
                ):
                    if payment.payment_method == Payment.Method.ONLINE:
                        # Online orders retain the pre-existing confirmation-gated
                        # lifecycle: their enrollment is created only after the
                        # provider has confirmed the payment.
                        conversion_enrollment = group_payments._enroll_paid_conversion_target(
                            club_id=club_id,
                            payment=payment,
                        )
                        if conversion_enrollment is not None:
                            payment.conversion_enrollment = conversion_enrollment
                            payment.save(update_fields=["conversion_enrollment", "updated_at"])
                    elif settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED:
                        raise BusinessLogicError(
                            "У ожидающей оплаты отсутствует операционная запись",
                            code="payment_conversion_enrollment_required",
                        )
                    else:
                        # Compatibility only: pre-release unlinked manual rows
                        # drain while new operational admission creation is off.
                        conversion_enrollment = group_payments._enroll_paid_conversion_target(
                            club_id=club_id,
                            payment=payment,
                        )
                        if conversion_enrollment is not None:
                            payment.conversion_enrollment = conversion_enrollment
                            payment.save(update_fields=["conversion_enrollment", "updated_at"])

                sale_earnings._capture_sale_earning_snapshots_for_components(
                    payment=payment,
                    subscription=sub,
                    club_id=club_id,
                )
                if payment.sale_earning_snapshot_recorded:
                    sale_earnings._enqueue_sale_earning_after_commit(
                        payment_id=payment.id,
                        club_id=club_id,
                    )
                elif any(
                    component.trainer_payout_policy_snapshot == Tariff.PayoutPolicy.ON_PAYMENT
                    for component in subscription_components
                ):
                    logger.warning(
                        "payment_on_payment_no_trainer_skip_earning",
                        extra={"payment_id": payment_id, "club_id": club_id},
                    )

                # Keep legacy group snapshot fill path only for old rows without
                # subscription components.
                if (
                    not subscription_components
                    and sub.tariff.training_type.kind == TrainingType.Kind.GROUP
                    and payment.seller_trainer_id
                ):
                    sale_earnings._capture_sale_earning_snapshot(
                        payment=payment,
                        subscription=sub,
                        club_id=club_id,
                    )

                from apps.attendance.models import PersonalServiceTermsSnapshot, complete_personal_terms_queryset

                complete_personal_terms = complete_personal_terms_queryset(
                    PersonalServiceTermsSnapshot.objects.for_club(club_id).filter(
                        models.Q(booking__payment_links__payment_id=payment.id)
                        | models.Q(reservation__payment_id=payment.id),
                    )
                ).exists()
                if complete_personal_terms:
                    from apps.leads.services import convert_lead_after_personal_payment_confirmation

                    convert_lead_after_personal_payment_confirmation(
                        club_id=club_id,
                        student_id=payment.student_id,
                        payment_id=payment.id,
                        actor_user_id=verified_by_id,
                    )
                else:
                    from apps.leads.services import convert_lead_after_subscription_payment

                    convert_lead_after_subscription_payment(
                        club_id=club_id,
                        student_id=payment.student_id,
                        actor_user_id=verified_by_id,
                    )
                from apps.attendance.services import reconcile_personal_drop_in_payment_after_verification

                reconcile_personal_drop_in_payment_after_verification(
                    payment_id=payment.id,
                    club_id=club_id,
                )
            else:
                logger.warning(
                    "payment_confirm_no_subscription_skip_debts",
                    extra={"payment_id": payment_id, "club_id": club_id},
                )

            logger.info(
                "payment_confirmed",
                extra={"id": payment_id, "club_id": club_id},
            )

        elif action == "reject":
            payment.status = Payment.Status.REJECTED
            payment.rejection_reason = rejection_reason
            payment.save(update_fields=["status", "rejection_reason", "updated_at"])
            group_payments._close_payment_owned_group_membership(
                payment=payment,
                club_id=club_id,
                actor_user_id=verified_by_id,
                rationale=rejection_reason,
            )

            if payment.subscription:
                payment.subscription.soft_delete()
            if payment.conversion_enrollment_id is not None:
                from apps.attendance.models import ScheduleEnrollment

                enrollment = (
                    ScheduleEnrollment.objects.for_club(club_id)
                    .select_for_update(of=("self",))
                    .get(id=payment.conversion_enrollment_id)
                )
                if enrollment.status in {
                    ScheduleEnrollment.Status.ACTIVE,
                    ScheduleEnrollment.Status.TRIAL,
                    ScheduleEnrollment.Status.FROZEN,
                }:
                    last_attended_date = (
                        Debt.objects.for_club(club_id)
                        .filter(
                            settlement_payment_id=payment.id,
                            checkin__schedule_id=enrollment.schedule_id,
                            checkin__deleted_at__isnull=True,
                            checkin__cancelled_at__isnull=True,
                        )
                        .order_by("-checkin__date", "-checkin__id")
                        .values_list("checkin__date", flat=True)
                        .first()
                    )
                    starts_on = enrollment.starts_on or payment.target_start_date
                    if starts_on is None:
                        raise BusinessLogicError(
                            "У операционной записи не указана дата старта",
                            code="payment_conversion_target_required",
                        )
                    enrollment.status = ScheduleEnrollment.Status.CANCELLED
                    enrollment.ends_on = max(starts_on, last_attended_date or starts_on)
                    enrollment.full_clean()
                    enrollment.save(update_fields=["status", "ends_on", "updated_at"])
            rejected_debt_ids = list(
                Debt.objects.for_club(club_id)
                .select_for_update(of=("self",))
                .select_related("student")
                .filter(
                    settlement_payment_id=payment.id,
                    resolved_at__isnull=True,
                )
            )
            record_debt_settlement_events(
                club_id=club_id,
                payment=payment,
                debt_ids=[debt.id for debt in rejected_debt_ids],
                event_type=DebtSettlementEvent.EventType.REJECTED,
            )
            for debt in rejected_debt_ids:
                record_debt_lifecycle_event(
                    club_id=club_id,
                    debt=debt,
                    event_type=DebtLifecycleEvent.EventType.REJECTED,
                    previous_state=debt_state(debt),
                    new_state="open",
                    actor_user_id=verified_by_id,
                    reason=rejection_reason,
                    payment_id=payment.id,
                    subscription_id=payment.subscription_id,
                )
            Debt.objects.for_club(club_id).filter(
                settlement_payment_id=payment.id,
                resolved_at__isnull=True,
            ).update(settlement_payment=None)

            # A flag-on personal manual attempt leaves a lead in its original
            # workspace until either review confirmation or an exact check-in.
            # Only an unattended rejection restores the captured follow-up;
            # attendance is irreversible and keeps the person active.
            from apps.attendance.models import PersonalDropInPaymentLink

            personal_link = (
                PersonalDropInPaymentLink.objects.for_club(club_id)
                .select_related("booking")
                .filter(payment_id=payment.id)
                .first()
            )
            if (
                personal_link is not None
                and personal_link.booking.state == personal_link.booking.State.SCHEDULED
            ):
                from apps.leads.services import restore_lead_after_terminal_personal_payment

                restore_lead_after_terminal_personal_payment(
                    club_id=club_id,
                    student_id=payment.student_id,
                    payment_id=payment.id,
                    actor_user_id=verified_by_id,
                    outcome="rejected",
                )

            if (
                payment.group_membership_action_snapshot
                == Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
                and payment.target_training_group_id is not None
            ):
                from apps.leads.services import restore_lead_after_terminal_group_payment

                restore_lead_after_terminal_group_payment(
                    club_id=club_id,
                    student_id=payment.student_id,
                    payment_id=payment.id,
                    actor_user_id=verified_by_id,
                    outcome="rejected",
                )

            logger.info(
                "payment_rejected",
                extra={"id": payment_id, "club_id": club_id},
            )

    if salary_checkin_ids:
        from django_q.tasks import async_task

        for checkin_id in salary_checkin_ids:
            async_task(
                "apps.attendance.tasks.calculate_salary",
                checkin_id,
                club_id=club_id,
            )

    return payment
