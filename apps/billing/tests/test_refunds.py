from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.attendance.models import (
    PersonalBookingPaymentReservation,
    ScheduleEnrollment,
    TrainingGroupMembership,
    TrainingGroupMembershipEvent,
    TrainingGroupRolloutState,
)
from apps.attendance.tests.factories import (
    CheckinFactory,
    ScheduleFactory,
    TrainingGroupFactory,
    TrainingGroupMembershipFactory,
    TrainingGroupRolloutStateFactory,
)
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentProviderEvent,
    Debt,
    Payment,
    PaymentRefund,
    PaymentRefundCase,
    Subscription,
    SubscriptionFreeze,
    Tariff,
    TrainingType,
)
from apps.billing.refund_services import (
    _revoke_payment_owned_group_membership,
    approve_payment_refund_case,
    complete_payment_refund_payroll,
)
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.common.exceptions import BusinessLogicError
from apps.dashboard.selectors import get_dashboard_metrics
from apps.dashboard.services import get_pnl_report
from apps.students.tests.factories import StudentFactory
from apps.trainers.models import (
    TrainerEarning,
    TrainerEarningAdjustment,
    TrainerPackageAllocation,
    TrainerPayrollPeriodClose,
)
from apps.trainers.selectors import (
    get_trainer_earnings_summary,
    get_trainer_payroll_adjustments,
    get_trainer_salary_ledger_rows,
)
from apps.trainers.services import correct_trainer_earning
from apps.trainers.tests.factories import TrainerFactory


def _refund_case(
    *,
    club,
    owner_user,
    amount: Decimal = Decimal("5000.00"),
    kind: str = "full",
    provider_event_id: str = "refund-event-1",
    received_at=None,
    training_kind: str = TrainingType.Kind.GROUP,
):
    received_at = received_at or timezone.now()
    student = StudentFactory(club=club, status="active")
    training_type = TrainingTypeFactory(club=club, kind=training_kind)
    tariff = TariffFactory(
        club=club,
        training_type=training_type,
        price=amount,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff=tariff,
        status=Subscription.Status.ACTIVE,
        paid_amount=amount,
    )
    payment = Payment.objects.create(
        club=club,
        student=student,
        tariff=tariff,
        subscription=subscription,
        amount=amount,
        original_amount=amount,
        payment_method=Payment.Method.ONLINE,
        status=Payment.Status.CONFIRMED,
        recorded_by=owner_user,
        verified_by=owner_user,
        verified_at=received_at,
    )
    order = BankPaymentOrder.objects.create(
        club=club,
        payment=payment,
        subscription=subscription,
        student=student,
        provider=BankPaymentOrder.Provider.MOCK,
        source=BankPaymentOrder.Source.OWNER,
        status=BankPaymentOrder.Status.MANUAL_REVIEW,
        amount_snapshot=amount,
        currency="RUB",
        purpose_snapshot="Test refund",
        provider_payment_link_id=f"rf-{provider_event_id}",
        expires_at=received_at + timedelta(days=1),
        created_by=owner_user,
    )
    event = BankPaymentProviderEvent.objects.create(
        club=club,
        order=order,
        provider=BankPaymentOrder.Provider.MOCK,
        event_type="acquiringInternetPayment",
        provider_event_id=provider_event_id,
        provider_payment_link_id=order.provider_payment_link_id,
        provider_status=("REFUNDED" if kind == "full" else "REFUNDED_PARTIALLY"),
        amount_snapshot=amount,
        received_at=received_at,
        processing_status=BankPaymentProviderEvent.ProcessingStatus.FAILED,
    )
    case = PaymentRefundCase.objects.create(
        club=club,
        order=order,
        provider_event=event,
        refund_kind=kind,
        detected_amount=amount if kind == "full" else None,
        provider_refunded_at=received_at,
        status=(
            PaymentRefundCase.Status.DETECTED
            if kind == "full"
            else PaymentRefundCase.Status.RECONCILIATION_REQUIRED
        ),
    )
    return order, case


@pytest.mark.django_db
def test_reconciling_blocks_refund_approval_without_resolving_case(club, owner_user):
    order, refund_case = _refund_case(club=club, owner_user=owner_user)
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club),
        mode=TrainingGroupRolloutState.Mode.RECONCILING
    )

    with pytest.raises(BusinessLogicError) as exc_info:
        approve_payment_refund_case(
            club_id=club.id,
            case_id=refund_case.id,
            actor_user_id=owner_user.id,
            idempotency_key="s6-reconciling-refund-gate",
            amount=order.payment.amount,
            refund_kind=PaymentRefund.Kind.FULL,
            reason="Refund must not race owner reconciliation.",
            entitlement_action=PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
        )

    assert exc_info.value.code == "training_group_reconciling"
    refund_case.refresh_from_db()
    assert refund_case.status == PaymentRefundCase.Status.DETECTED


def _refund_case_for_existing_payment(
    *,
    club,
    owner_user,
    payment: Payment,
    provider_event_id: str,
) -> tuple[BankPaymentOrder, PaymentRefundCase]:
    received_at = timezone.now()
    order = BankPaymentOrder.objects.create(
        club=club,
        payment=payment,
        subscription=payment.subscription,
        student=payment.student,
        provider=BankPaymentOrder.Provider.MOCK,
        source=BankPaymentOrder.Source.OWNER,
        status=BankPaymentOrder.Status.MANUAL_REVIEW,
        amount_snapshot=payment.amount,
        currency="RUB",
        purpose_snapshot="Test group renewal refund",
        provider_payment_link_id=f"rf-{provider_event_id}",
        expires_at=received_at + timedelta(days=1),
        created_by=owner_user,
    )
    event = BankPaymentProviderEvent.objects.create(
        club=club,
        order=order,
        provider=BankPaymentOrder.Provider.MOCK,
        event_type="acquiringInternetPayment",
        provider_event_id=provider_event_id,
        provider_payment_link_id=order.provider_payment_link_id,
        provider_status="REFUNDED",
        amount_snapshot=payment.amount,
        received_at=received_at,
        processing_status=BankPaymentProviderEvent.ProcessingStatus.FAILED,
    )
    return order, PaymentRefundCase.objects.create(
        club=club,
        order=order,
        provider_event=event,
        refund_kind=PaymentRefundCase.Kind.FULL,
        detected_amount=payment.amount,
        provider_refunded_at=received_at,
        status=PaymentRefundCase.Status.DETECTED,
    )


def _attach_group_membership_to_refund_payment(
    *,
    payment: Payment,
    authority: str = TrainingGroupMembership.Authority.PAYMENT_OWNED,
    status: str = TrainingGroupMembership.Status.ACTIVE,
) -> TrainingGroupMembership:
    group = TrainingGroupFactory(
        club=payment.club,
        training_type=payment.tariff.training_type,
    )
    membership = TrainingGroupMembershipFactory(
        club=payment.club,
        student=payment.student,
        training_group=group,
        authority=authority,
        status=status,
        source=(
            TrainingGroupMembership.Source.PAID_CONVERSION
            if authority == TrainingGroupMembership.Authority.PAYMENT_OWNED
            else TrainingGroupMembership.Source.MANUAL
        ),
    )
    payment.target_training_group = group
    payment.target_group_membership = membership
    payment.conversion_group_membership = membership
    payment.group_membership_action_snapshot = Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
    payment.save(
        update_fields=[
            "target_training_group",
            "target_group_membership",
            "conversion_group_membership",
            "group_membership_action_snapshot",
            "updated_at",
        ]
    )
    return membership


def _renewal_payment_for_group_membership(
    *,
    club,
    owner_user,
    owner_payment: Payment,
    membership: TrainingGroupMembership,
) -> Payment:
    subscription = SubscriptionFactory(
        club=club,
        student=owner_payment.student,
        tariff=owner_payment.tariff,
        status=Subscription.Status.ACTIVE,
        paid_amount=owner_payment.amount,
    )
    return Payment.objects.create(
        club=club,
        student=owner_payment.student,
        tariff=owner_payment.tariff,
        subscription=subscription,
        amount=owner_payment.amount,
        original_amount=owner_payment.amount,
        payment_method=Payment.Method.ONLINE,
        status=Payment.Status.CONFIRMED,
        recorded_by=owner_user,
        verified_by=owner_user,
        verified_at=timezone.now(),
        target_training_group_id=membership.training_group_id,
        target_group_membership=membership,
        group_membership_action_snapshot=Payment.GroupMembershipActionSnapshot.RENEWAL,
    )


@pytest.mark.django_db
def test_public_owner_refund_defers_to_confirmed_renewal_then_last_refund_revokes_group_membership(
    club,
    owner_user,
):
    owner_order, owner_case = _refund_case(
        club=club,
        owner_user=owner_user,
        provider_event_id="group-owner-refund",
    )
    membership = _attach_group_membership_to_refund_payment(payment=owner_order.payment)
    TrainingGroupRolloutStateFactory(club=club)
    renewal_payment = _renewal_payment_for_group_membership(
        club=club,
        owner_user=owner_user,
        owner_payment=owner_order.payment,
        membership=membership,
    )
    _, renewal_case = _refund_case_for_existing_payment(
        club=club,
        owner_user=owner_user,
        payment=renewal_payment,
        provider_event_id="group-renewal-refund",
    )

    owner_refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=owner_case.id,
        actor_user_id=owner_user.id,
        idempotency_key="public-group-owner-full-revoke",
        amount=owner_order.payment.amount,
        refund_kind=PaymentRefund.Kind.FULL,
        reason="Provider refunded original group admission",
        entitlement_action=PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
    )

    membership.refresh_from_db()
    assert owner_refund.enrollment_disposition == PaymentRefund.EnrollmentDisposition.KEPT
    assert membership.status == TrainingGroupMembership.Status.ACTIVE
    deferred_event = TrainingGroupMembershipEvent.objects.for_club(club).get(
        membership=membership,
        action="payment_refund_deferred",
    )
    assert deferred_event.source_payment_id == owner_order.payment_id

    renewal_refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=renewal_case.id,
        actor_user_id=owner_user.id,
        idempotency_key="public-group-renewal-last-support-refund",
        amount=renewal_payment.amount,
        refund_kind=PaymentRefund.Kind.FULL,
        reason="Provider refunded final group renewal support",
        entitlement_action=PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
    )

    membership.refresh_from_db()
    assert renewal_refund.enrollment_disposition == (
        PaymentRefund.EnrollmentDisposition.CANCELLED_PAYMENT_CREATED
    )
    assert membership.status == TrainingGroupMembership.Status.CANCELLED
    assert TrainingGroupMembershipEvent.objects.for_club(club).filter(
        membership=membership,
        action="payment_cancelled",
        source_payment_id=owner_order.payment_id,
    ).exists()


@pytest.mark.django_db
def test_public_owner_full_revoke_cancels_unrenewed_payment_owned_group_membership(
    club,
    owner_user,
    settings,
):
    order, case = _refund_case(
        club=club,
        owner_user=owner_user,
        provider_event_id="group-owner-unrenewed-refund",
    )
    membership = _attach_group_membership_to_refund_payment(payment=order.payment)
    TrainingGroupRolloutStateFactory(club=club)
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = False

    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=case.id,
        actor_user_id=owner_user.id,
        idempotency_key="public-group-owner-unrenewed-full-revoke",
        amount=order.payment.amount,
        refund_kind=PaymentRefund.Kind.FULL,
        reason="Provider refunded unrenewed group admission",
        entitlement_action=PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
    )

    membership.refresh_from_db()
    assert refund.enrollment_disposition == (
        PaymentRefund.EnrollmentDisposition.CANCELLED_PAYMENT_CREATED
    )
    assert membership.status == TrainingGroupMembership.Status.CANCELLED


@pytest.mark.django_db
def test_public_owner_full_revoke_closes_every_payment_owned_group_projection(
    club,
    owner_user,
):
    from apps.attendance.services.training_group_memberships import (
        create_payment_owned_training_group_membership,
    )

    order, case = _refund_case(
        club=club,
        owner_user=owner_user,
        provider_event_id="group-owner-multislot-refund",
    )
    group = TrainingGroupFactory(
        club=club,
        training_type=order.payment.tariff.training_type,
    )
    for day_of_week in (0, 2):
        ScheduleFactory(
            club=club,
            training_group=group,
            trainer=group.responsible_trainer,
            training_type=group.training_type,
            location=group.location,
            day_of_week=day_of_week,
        )
    TrainingGroupRolloutStateFactory(club=club)
    membership, projections = create_payment_owned_training_group_membership(
        club_id=club.id,
        student_id=order.student_id,
        training_group_id=group.id,
        starts_on=timezone.localdate(),
        payment_id=order.payment_id,
        actor_user_id=owner_user.id,
    )
    order.payment.target_training_group = group
    order.payment.target_group_membership = membership
    order.payment.conversion_group_membership = membership
    order.payment.group_membership_action_snapshot = Payment.GroupMembershipActionSnapshot.NEW_ADMISSION
    order.payment.save(
        update_fields=[
            "target_training_group",
            "target_group_membership",
            "conversion_group_membership",
            "group_membership_action_snapshot",
            "updated_at",
        ]
    )

    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=case.id,
        actor_user_id=owner_user.id,
        idempotency_key="public-group-owner-multislot-full-revoke",
        amount=order.payment.amount,
        refund_kind=PaymentRefund.Kind.FULL,
        reason="Provider refunded unrenewed multi-slot group admission",
        entitlement_action=PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
    )

    membership.refresh_from_db()
    assert refund.enrollment_disposition == (
        PaymentRefund.EnrollmentDisposition.CANCELLED_PAYMENT_CREATED
    )
    assert membership.status == TrainingGroupMembership.Status.CANCELLED
    assert len(projections) == 2
    for projection in projections:
        projection.refresh_from_db()
        assert projection.created_from == ScheduleEnrollment.CreatedFrom.GROUP_PROJECTION
        assert projection.status == ScheduleEnrollment.Status.CANCELLED
        assert projection.ends_on == membership.ends_on


@pytest.mark.django_db
def test_public_renewal_refund_without_owner_revoke_keeps_group_membership(
    club,
    owner_user,
):
    owner_order, _ = _refund_case(
        club=club,
        owner_user=owner_user,
        provider_event_id="group-owner-still-supported",
    )
    membership = _attach_group_membership_to_refund_payment(payment=owner_order.payment)
    TrainingGroupRolloutStateFactory(club=club)
    renewal_payment = _renewal_payment_for_group_membership(
        club=club,
        owner_user=owner_user,
        owner_payment=owner_order.payment,
        membership=membership,
    )
    _, renewal_case = _refund_case_for_existing_payment(
        club=club,
        owner_user=owner_user,
        payment=renewal_payment,
        provider_event_id="group-renewal-alone-refund",
    )

    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=renewal_case.id,
        actor_user_id=owner_user.id,
        idempotency_key="public-group-renewal-alone-full-revoke",
        amount=renewal_payment.amount,
        refund_kind=PaymentRefund.Kind.FULL,
        reason="Provider refunded renewal while owner remains supported",
        entitlement_action=PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
    )

    membership.refresh_from_db()
    assert refund.enrollment_disposition == PaymentRefund.EnrollmentDisposition.KEPT
    assert membership.status == TrainingGroupMembership.Status.ACTIVE
    assert not TrainingGroupMembershipEvent.objects.for_club(club).filter(
        membership=membership,
        action__in=["payment_refund_deferred", "payment_cancelled"],
    ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("refund_kind", "refund_amount", "entitlement_action", "expected_disposition"),
    [
        (
            PaymentRefund.Kind.PARTIAL,
            Decimal("1000.00"),
            None,
            PaymentRefund.EntitlementDisposition.KEPT_PARTIAL,
        ),
        (
            PaymentRefund.Kind.FULL,
            Decimal("5000.00"),
            PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
            PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
        ),
    ],
)
def test_public_partial_and_keep_refunds_preserve_group_membership(
    club,
    owner_user,
    refund_kind,
    refund_amount,
    entitlement_action,
    expected_disposition,
):
    order, case = _refund_case(
        club=club,
        owner_user=owner_user,
        kind=refund_kind,
        provider_event_id=f"group-{refund_kind}-keep",
    )
    membership = _attach_group_membership_to_refund_payment(payment=order.payment)
    TrainingGroupRolloutStateFactory(club=club)

    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=case.id,
        actor_user_id=owner_user.id,
        idempotency_key=f"public-group-{refund_kind}-keep",
        amount=refund_amount,
        refund_kind=refund_kind,
        reason="Provider refund keeps group operational admission",
        entitlement_action=entitlement_action,
    )

    membership.refresh_from_db()
    assert refund.entitlement_disposition == expected_disposition
    assert membership.status == TrainingGroupMembership.Status.ACTIVE
    assert not TrainingGroupMembershipEvent.objects.for_club(club).filter(
        membership=membership,
        action__in=["payment_refund_deferred", "payment_cancelled"],
    ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("authority", "status", "mismatched_owner_link"),
    [
        (TrainingGroupMembership.Authority.INDEPENDENT, TrainingGroupMembership.Status.ACTIVE, False),
        (TrainingGroupMembership.Authority.PAYMENT_OWNED, TrainingGroupMembership.Status.TRANSFERRED, False),
        (TrainingGroupMembership.Authority.PAYMENT_OWNED, TrainingGroupMembership.Status.ACTIVE, True),
    ],
)
def test_public_full_revoke_preserves_independent_transferred_and_mismatched_group_membership(
    club,
    owner_user,
    authority,
    status,
    mismatched_owner_link,
):
    order, case = _refund_case(
        club=club,
        owner_user=owner_user,
        provider_event_id=f"group-{authority}-{status}-refund",
    )
    membership = _attach_group_membership_to_refund_payment(
        payment=order.payment,
        authority=authority,
        status=status,
    )
    if mismatched_owner_link:
        Payment.objects.for_club(club).filter(id=order.payment_id).update(
            target_group_membership=None
        )
    TrainingGroupRolloutStateFactory(club=club)

    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=case.id,
        actor_user_id=owner_user.id,
        idempotency_key=f"public-group-{authority}-{status}-full-revoke",
        amount=order.payment.amount,
        refund_kind=PaymentRefund.Kind.FULL,
        reason="Provider refund must not adopt independent or transferred membership",
        entitlement_action=PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
    )

    membership.refresh_from_db()
    assert refund.enrollment_disposition == PaymentRefund.EnrollmentDisposition.KEPT
    assert membership.status == status
    assert not TrainingGroupMembershipEvent.objects.for_club(club).filter(
        membership=membership,
        action="payment_cancelled",
    ).exists()


def _sale_earning(
    *,
    club,
    payment,
    trainer,
    amount: Decimal,
    subscription_component=None,
) -> TrainerEarning:
    return TrainerEarning.objects.create(
        club=club,
        trainer=trainer,
        payment=payment,
        subscription_component=subscription_component,
        earning_source=TrainerEarning.Source.SALE,
        earning_type=TrainerEarning.EarningType.GROUP,
        amount=amount,
        rate_percent=Decimal("20.00"),
        subscription_price=payment.amount,
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("membership_authority", "membership_status", "renewal_support", "expected_disposition", "expected_status"),
    [
        (
            TrainingGroupMembership.Authority.PAYMENT_OWNED,
            TrainingGroupMembership.Status.ACTIVE,
            False,
            PaymentRefund.EnrollmentDisposition.CANCELLED_PAYMENT_CREATED,
            TrainingGroupMembership.Status.CANCELLED,
        ),
        (
            TrainingGroupMembership.Authority.PAYMENT_OWNED,
            TrainingGroupMembership.Status.ACTIVE,
            True,
            PaymentRefund.EnrollmentDisposition.KEPT,
            TrainingGroupMembership.Status.ACTIVE,
        ),
        (
            TrainingGroupMembership.Authority.INDEPENDENT,
            TrainingGroupMembership.Status.ACTIVE,
            False,
            PaymentRefund.EnrollmentDisposition.KEPT,
            TrainingGroupMembership.Status.ACTIVE,
        ),
        (
            TrainingGroupMembership.Authority.PAYMENT_OWNED,
            TrainingGroupMembership.Status.TRANSFERRED,
            False,
            PaymentRefund.EnrollmentDisposition.KEPT,
            TrainingGroupMembership.Status.TRANSFERRED,
        ),
    ],
)
def test_group_refund_revokes_only_last_eligible_payment_owned_support(
    club,
    owner_user,
    membership_authority,
    membership_status,
    renewal_support,
    expected_disposition,
    expected_status,
):
    from apps.attendance.models import TrainingGroupRolloutState

    group = TrainingGroupFactory(club=club)
    membership = TrainingGroupMembershipFactory(
        club=club,
        student=StudentFactory(club=club),
        training_group=group,
        authority=membership_authority,
        status=membership_status,
    )
    rollout = TrainingGroupRolloutStateFactory(club=club)
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.SHADOW
    )
    owner_payment = PaymentFactory(
        club=club,
        student=membership.student,
        target_training_group=group,
        target_group_membership=membership,
        conversion_group_membership=membership,
        group_membership_action_snapshot=Payment.GroupMembershipActionSnapshot.NEW_ADMISSION,
        status=Payment.Status.CONFIRMED,
    )
    if renewal_support:
        PaymentFactory(
            club=club,
            student=membership.student,
            target_training_group=group,
            target_group_membership=membership,
            group_membership_action_snapshot=Payment.GroupMembershipActionSnapshot.RENEWAL,
            status=Payment.Status.CONFIRMED,
        )

    disposition = _revoke_payment_owned_group_membership(
        club_id=club.id,
        payment=owner_payment,
        actor_user_id=owner_user.id,
        reason="full provider refund",
    )

    membership.refresh_from_db()
    assert disposition == expected_disposition
    assert membership.status == expected_status


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("refund_kind", "refund_amount", "entitlement_action", "expected_debit"),
    [
        (PaymentRefund.Kind.PARTIAL, Decimal("1000.00"), None, Decimal("-200.00")),
        (
            PaymentRefund.Kind.FULL,
            Decimal("5000.00"),
            PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
            Decimal("-1000.00"),
        ),
    ],
)
def test_refund_materializes_delayed_sale_earning_before_completing_payroll(
    club,
    owner_user,
    refund_kind,
    refund_amount,
    entitlement_action,
    expected_debit,
):
    order, case = _refund_case(
        club=club,
        owner_user=owner_user,
        kind=refund_kind,
    )
    trainer = TrainerFactory(club=club)
    payment = order.payment
    payment.seller_trainer = trainer
    payment.sale_earning_snapshot_recorded = True
    payment.sale_trainer_id_snapshot = trainer.id
    payment.sale_training_type_id_snapshot = payment.tariff.training_type_id
    payment.sale_training_type_kind_snapshot = TrainingType.Kind.GROUP
    payment.sale_rate_percent_snapshot = Decimal("20.00")
    payment.sale_amount_basis_snapshot = payment.amount
    payment.sale_snapshot_provenance = Payment.SaleSnapshotProvenance.CONFIRM_TIME
    payment.save(
        update_fields=[
            "seller_trainer",
            "sale_earning_snapshot_recorded",
            "sale_trainer_id_snapshot",
            "sale_training_type_id_snapshot",
            "sale_training_type_kind_snapshot",
            "sale_rate_percent_snapshot",
            "sale_amount_basis_snapshot",
            "sale_snapshot_provenance",
            "updated_at",
        ]
    )
    assert not TrainerEarning.objects.for_club(club).filter(payment=payment).exists()

    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=case.id,
        actor_user_id=owner_user.id,
        idempotency_key=f"refund-before-sale-worker-{refund_kind}",
        amount=refund_amount,
        refund_kind=refund_kind,
        reason="Provider refund arrived before salary worker",
        entitlement_action=entitlement_action,
    )

    from apps.billing.tasks import create_sale_earning

    create_sale_earning(payment.id, club.id)
    earning = TrainerEarning.objects.for_club(club).get(payment=payment)
    adjustment = TrainerEarningAdjustment.objects.for_club(club).get(
        source_refund=refund,
        source_earning=earning,
    )
    assert refund.status == PaymentRefund.Status.COMPLETED
    assert earning.amount == Decimal("1000.00")
    assert adjustment.trainer_id == trainer.id
    assert adjustment.payable_amount_delta == expected_debit
    assert TrainerEarning.objects.for_club(club).filter(payment=payment).count() == 1


@pytest.mark.django_db
def test_refund_skips_sale_earning_when_payment_policy_has_no_on_payment_payout(
    club,
    owner_user,
):
    order, case = _refund_case(
        club=club,
        owner_user=owner_user,
        kind=PaymentRefund.Kind.FULL,
    )
    trainer = TrainerFactory(club=club)
    payment = order.payment
    payment.seller_trainer = trainer
    payment.save(update_fields=["seller_trainer", "updated_at"])
    payment.tariff.trainer_payout_policy = Tariff.PayoutPolicy.NONE
    payment.tariff.save(update_fields=["trainer_payout_policy", "updated_at"])
    SubscriptionComponentFactory(
        club=club,
        subscription=order.subscription,
        trainer_payout_policy_snapshot=Tariff.PayoutPolicy.NONE,
        tariff_component__trainer_payout_policy=Tariff.PayoutPolicy.NONE,
    )

    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=case.id,
        actor_user_id=owner_user.id,
        idempotency_key="refund-without-on-payment-payout",
        amount=payment.amount,
        refund_kind=PaymentRefund.Kind.FULL,
        reason="Provider refund without sale compensation",
        entitlement_action=PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
    )

    assert refund.status == PaymentRefund.Status.COMPLETED
    assert not TrainerEarning.objects.for_club(club).filter(payment=payment).exists()
    assert not TrainerEarningAdjustment.objects.for_club(club).filter(
        source_refund=refund,
    ).exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    (
        "refund_kind",
        "refund_amount",
        "entitlement_action",
        "expected_source_debit",
        "expected_target_debit",
    ),
    [
        (
            PaymentRefund.Kind.PARTIAL,
            Decimal("2500.00"),
            None,
            Decimal("-300.00"),
            Decimal("-200.00"),
        ),
        (
            PaymentRefund.Kind.FULL,
            Decimal("5000.00"),
            PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
            Decimal("-600.00"),
            Decimal("-400.00"),
        ),
    ],
)
def test_refund_follows_manual_sale_earning_correction_between_trainers(
    club,
    owner_user,
    refund_kind,
    refund_amount,
    entitlement_action,
    expected_source_debit,
    expected_target_debit,
):
    order, case = _refund_case(
        club=club,
        owner_user=owner_user,
        kind=refund_kind,
    )
    source_trainer = TrainerFactory(club=club)
    target_trainer = TrainerFactory(club=club)
    earning = _sale_earning(
        club=club,
        payment=order.payment,
        trainer=source_trainer,
        amount=Decimal("1000.00"),
    )
    correction = correct_trainer_earning(
        club_id=club.id,
        earning_id=earning.id,
        target_trainer_id=target_trainer.id,
        amount=Decimal("400.00"),
        reason="Correct sale attribution",
        actor_user_id=owner_user.id,
        idempotency_key="sale-owner-transfer-before-refund",
    )

    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=case.id,
        actor_user_id=owner_user.id,
        idempotency_key=f"refund-after-sale-owner-transfer-{refund_kind}",
        amount=refund_amount,
        refund_kind=refund_kind,
        reason="Provider refunded the payment",
        entitlement_action=entitlement_action,
    )

    refund_debits = {
        row.trainer_id: row.payable_amount_delta
        for row in TrainerEarningAdjustment.objects.for_club(club).filter(
            source_refund=refund,
            source_earning=earning,
        )
    }
    correction.debit.refresh_from_db()
    correction.credit.refresh_from_db()
    assert correction.debit.source_earning_id == earning.id
    assert correction.credit.source_earning_id == earning.id
    assert refund_debits == {
        source_trainer.id: expected_source_debit,
        target_trainer.id: expected_target_debit,
    }


@pytest.mark.django_db
def test_manual_sale_earning_correction_is_rejected_after_refund_adjustment(
    club,
    owner_user,
):
    order, case = _refund_case(
        club=club,
        owner_user=owner_user,
        kind=PaymentRefund.Kind.PARTIAL,
    )
    source_trainer = TrainerFactory(club=club)
    target_trainer = TrainerFactory(club=club)
    earning = _sale_earning(
        club=club,
        payment=order.payment,
        trainer=source_trainer,
        amount=Decimal("1000.00"),
    )
    approve_payment_refund_case(
        club_id=club.id,
        case_id=case.id,
        actor_user_id=owner_user.id,
        idempotency_key="refund-before-sale-owner-transfer",
        amount=Decimal("1000.00"),
        refund_kind=PaymentRefund.Kind.PARTIAL,
        reason="Provider refund",
    )

    with pytest.raises(BusinessLogicError) as exc:
        correct_trainer_earning(
            club_id=club.id,
            earning_id=earning.id,
            target_trainer_id=target_trainer.id,
            amount=Decimal("400.00"),
            reason="Late sale attribution correction",
            actor_user_id=owner_user.id,
            idempotency_key="sale-owner-transfer-after-refund",
        )

    assert exc.value.code == "trainer_earning_refund_adjusted"


@pytest.mark.django_db
def test_partial_refund_posts_net_revenue_and_preserves_entitlement_debt_and_history(
    club,
    owner_user,
):
    order, case = _refund_case(
        club=club,
        owner_user=owner_user,
        kind=PaymentRefund.Kind.PARTIAL,
    )
    component = SubscriptionComponentFactory(
        club=club,
        subscription=order.subscription,
        credits_total=8,
        credits_left=5,
        credits_used=3,
    )
    trainer = TrainerFactory(club=club)
    allocation = TrainerPackageAllocation.objects.create(
        club=club,
        subscription=order.subscription,
        payment=order.payment,
        student=order.student,
        tariff=order.payment.tariff,
        training_type=order.payment.tariff.training_type,
        owner_trainer=trainer,
        source=TrainerPackageAllocation.Source.PAYMENT,
        sessions_total_snapshot=8,
        sessions_remaining_snapshot=5,
        amount_snapshot=order.payment.amount,
        is_active=True,
        activated_at=timezone.now(),
        created_by=owner_user,
    )
    earning = _sale_earning(
        club=club,
        payment=order.payment,
        trainer=trainer,
        amount=Decimal("1000.00"),
    )
    checkin = CheckinFactory(
        club=club,
        student=order.student,
        training_type=order.payment.tariff.training_type,
        subscription=order.subscription,
        is_debt=True,
    )
    debt = Debt.objects.create(
        club=club,
        student=order.student,
        checkin=checkin,
        tariff_price=Decimal("750.00"),
        reason="no_subscription",
        settlement_payment=order.payment,
        resolved_at=timezone.now(),
        resolution_type="payment",
    )

    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=case.id,
        actor_user_id=owner_user.id,
        idempotency_key="partial-refund-1",
        amount=Decimal("1000.00"),
        refund_kind=PaymentRefund.Kind.PARTIAL,
        reason="Provider statement reconciled",
    )

    order.refresh_from_db()
    order.payment.refresh_from_db()
    order.subscription.refresh_from_db()
    component.refresh_from_db()
    allocation.refresh_from_db()
    debt.refresh_from_db()
    adjustment = TrainerEarningAdjustment.objects.get(
        source_refund=refund,
        source_earning=earning,
    )
    pnl = get_pnl_report(
        club=club,
        date_from=refund.accounting_date,
        date_to=refund.accounting_date,
    )

    assert order.status == BankPaymentOrder.Status.REFUNDED_PARTIALLY
    assert order.payment.status == Payment.Status.CONFIRMED
    assert order.subscription.status == Subscription.Status.ACTIVE
    assert component.is_active is True
    assert component.credits_left == 5
    assert allocation.is_active is True
    assert debt.resolved_at is not None
    assert debt.settlement_payment_id == order.payment_id
    assert refund.entitlement_disposition == PaymentRefund.EntitlementDisposition.KEPT_PARTIAL
    assert refund.settled_debt_disposition == PaymentRefund.SettledDebtDisposition.ABSORBED
    assert refund.settled_debts_snapshot == [
        {"debt_id": debt.id, "amount": "750.00"},
    ]
    assert adjustment.kind == TrainerEarningAdjustment.Kind.REFUND
    assert adjustment.payable_amount_delta == Decimal("-200.00")
    assert pnl["gross_income"] == Decimal("5000.00")
    assert pnl["refunded_income"] == Decimal("1000.00")
    assert pnl["income"] == Decimal("4000.00")
    assert get_dashboard_metrics(
        club=club,
        date_from=refund.accounting_date,
        date_to=refund.accounting_date,
    )["revenue"] == Decimal("4000.00")
    ledger_rows = get_trainer_salary_ledger_rows(
        club=club,
        trainer_id=trainer.id,
        date_from=refund.accounting_date,
        date_to=refund.accounting_date,
    )
    refund_row = next(row for row in ledger_rows if row.row_type == "adjustment")
    assert refund_row.earning_type == TrainerEarningAdjustment.Kind.REFUND
    assert refund_row.amount == Decimal("-200.00")
    summary = get_trainer_earnings_summary(
        club=club,
        trainer_id=trainer.id,
        date_from=refund.accounting_date,
        date_to=refund.accounting_date,
    )
    assert summary["by_type"][TrainerEarningAdjustment.Kind.REFUND] == {
        "count": 1,
        "total": Decimal("-200.00"),
    }


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("entitlement_action", "expected_status", "expected_disposition"),
    [
        (
            PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
            Subscription.Status.ACTIVE,
            PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
        ),
        (
            PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
            Subscription.Status.CANCELLED,
            PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
        ),
    ],
)
def test_full_refund_requires_and_records_explicit_entitlement_disposition(
    club,
    owner_user,
    entitlement_action,
    expected_status,
    expected_disposition,
):
    order, case = _refund_case(club=club, owner_user=owner_user)

    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=case.id,
        actor_user_id=owner_user.id,
        idempotency_key=f"full-{entitlement_action}",
        amount=order.payment.amount,
        refund_kind=PaymentRefund.Kind.FULL,
        entitlement_action=entitlement_action,
        legacy_enrollment_action="leave_unlinked",
        reason="Full provider refund",
    )

    order.subscription.refresh_from_db()
    assert order.subscription.status == expected_status
    assert refund.entitlement_disposition == expected_disposition
    assert refund.status == PaymentRefund.Status.COMPLETED


@pytest.mark.django_db
def test_full_revoke_closes_only_payment_owned_artifacts_and_freezes(
    club,
    owner_user,
):
    order, case = _refund_case(club=club, owner_user=owner_user)
    component = SubscriptionComponentFactory(club=club, subscription=order.subscription)
    trainer = TrainerFactory(club=club)
    allocation = TrainerPackageAllocation.objects.create(
        club=club,
        subscription=order.subscription,
        payment=order.payment,
        student=order.student,
        tariff=order.payment.tariff,
        training_type=order.payment.tariff.training_type,
        owner_trainer=trainer,
        source=TrainerPackageAllocation.Source.PAYMENT,
        amount_snapshot=order.payment.amount,
        is_active=True,
        activated_at=timezone.now(),
        created_by=owner_user,
    )
    pending_freeze = SubscriptionFreeze.objects.create(
        club=club,
        subscription=order.subscription,
        days=7,
        reason=SubscriptionFreeze.Reason.VACATION,
        status=SubscriptionFreeze.FreezeStatus.PENDING,
        frozen_by=owner_user,
        starts_at=timezone.now(),
    )
    approved_freeze = SubscriptionFreeze.objects.create(
        club=club,
        subscription=order.subscription,
        days=7,
        reason=SubscriptionFreeze.Reason.VACATION,
        status=SubscriptionFreeze.FreezeStatus.APPROVED,
        frozen_by=owner_user,
        approved_by=owner_user,
        decision_at=timezone.now(),
        starts_at=timezone.now(),
    )
    schedule = ScheduleFactory(
        club=club,
        training_type=order.payment.tariff.training_type,
    )
    refund_date = timezone.localdate() + timedelta(days=2)
    enrollment = ScheduleEnrollment.objects.create(
        club=club,
        student=order.student,
        schedule=schedule,
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=refund_date,
        created_from=ScheduleEnrollment.CreatedFrom.PAID_CONVERSION,
    )
    order.payment.target_schedule = schedule
    order.payment.target_start_date = refund_date
    order.payment.conversion_enrollment = enrollment
    order.payment.save(
        update_fields=[
            "target_schedule",
            "target_start_date",
            "conversion_enrollment",
            "updated_at",
        ]
    )
    unrelated = ScheduleEnrollment.objects.create(
        club=club,
        student=order.student,
        schedule=ScheduleFactory(club=club),
        status=ScheduleEnrollment.Status.ACTIVE,
        created_from=ScheduleEnrollment.CreatedFrom.MANUAL,
    )

    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=case.id,
        actor_user_id=owner_user.id,
        idempotency_key="full-revoke-artifacts",
        amount=order.payment.amount,
        refund_kind=PaymentRefund.Kind.FULL,
        entitlement_action=PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
        reason="Revoke unused entitlement",
    )

    order.subscription.refresh_from_db()
    component.refresh_from_db()
    allocation.refresh_from_db()
    pending_freeze.refresh_from_db()
    approved_freeze.refresh_from_db()
    enrollment.refresh_from_db()
    unrelated.refresh_from_db()
    assert order.subscription.status == Subscription.Status.CANCELLED
    assert component.is_active is False
    assert allocation.is_active is False
    assert allocation.deactivated_by_id == owner_user.id
    assert pending_freeze.status == SubscriptionFreeze.FreezeStatus.REJECTED
    assert approved_freeze.ends_at is not None
    assert enrollment.status == ScheduleEnrollment.Status.CANCELLED
    assert unrelated.status == ScheduleEnrollment.Status.ACTIVE
    assert refund.enrollment_disposition == PaymentRefund.EnrollmentDisposition.CANCELLED_PAYMENT_CREATED


@pytest.mark.django_db
@pytest.mark.parametrize("delivered", [False, True])
def test_full_revoke_cancels_future_personal_booking_but_keeps_delivered_history(
    club,
    owner_user,
    delivered,
):
    order, case = _refund_case(
        club=club,
        owner_user=owner_user,
        training_kind=TrainingType.Kind.PERSONAL,
    )
    target_date = timezone.localdate() + timedelta(days=-1 if delivered else 2)
    schedule = ScheduleFactory(
        club=club,
        training_type=order.payment.tariff.training_type,
        one_time_date=target_date,
        is_active=True,
    )
    enrollment = ScheduleEnrollment.objects.create(
        club=club,
        student=order.student,
        schedule=schedule,
        status=ScheduleEnrollment.Status.ACTIVE,
        starts_on=target_date,
        ends_on=target_date,
        created_from=ScheduleEnrollment.CreatedFrom.PERSONAL_BOOKING,
    )
    starts_at = timezone.make_aware(
        datetime.combine(target_date, schedule.start_time),
    )
    ends_at = timezone.make_aware(
        datetime.combine(target_date, schedule.end_time),
    )
    reservation = PersonalBookingPaymentReservation.objects.create(
        club=club,
        student=order.student,
        trainer=schedule.trainer,
        location=schedule.location,
        training_type=schedule.training_type,
        tariff=order.payment.tariff,
        payment=order.payment,
        bank_payment_order=order,
        subscription=order.subscription,
        schedule=schedule,
        enrollment=enrollment,
        starts_at=starts_at,
        ends_at=ends_at,
        status=PersonalBookingPaymentReservation.Status.BOOKED,
        expires_at=starts_at,
        idempotency_key=f"refund-personal-{delivered}",
        created_by=owner_user,
    )

    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=case.id,
        actor_user_id=owner_user.id,
        idempotency_key=f"full-personal-{delivered}",
        amount=order.payment.amount,
        refund_kind=PaymentRefund.Kind.FULL,
        entitlement_action=PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
        reason="Full personal booking refund",
    )

    enrollment.refresh_from_db()
    reservation.refresh_from_db()
    schedule.refresh_from_db()
    if delivered:
        assert enrollment.status == ScheduleEnrollment.Status.ACTIVE
        assert reservation.status == PersonalBookingPaymentReservation.Status.BOOKED
        assert schedule.is_active is True
        assert refund.personal_booking_disposition == (
            PaymentRefund.PersonalBookingDisposition.DELIVERED_HISTORY_KEPT
        )
    else:
        assert enrollment.status == ScheduleEnrollment.Status.CANCELLED
        assert reservation.status == PersonalBookingPaymentReservation.Status.CANCELLED
        assert schedule.is_active is False
        assert refund.personal_booking_disposition == (
            PaymentRefund.PersonalBookingDisposition.CANCELLED_FUTURE
        )


@pytest.mark.django_db
def test_cumulative_partials_reaching_full_require_full_action_and_round_exactly(
    club,
    owner_user,
):
    order, first_case = _refund_case(
        club=club,
        owner_user=owner_user,
        amount=Decimal("1000.00"),
        kind=PaymentRefund.Kind.PARTIAL,
        provider_event_id="partial-1",
    )
    trainer = TrainerFactory(club=club)
    components = [
        SubscriptionComponentFactory(club=club, subscription=order.subscription),
        SubscriptionComponentFactory(club=club, subscription=order.subscription),
    ]
    earnings = [
        _sale_earning(
            club=club,
            payment=order.payment,
            trainer=trainer,
            amount=Decimal("333.33"),
            subscription_component=components[0],
        ),
        _sale_earning(
            club=club,
            payment=order.payment,
            trainer=trainer,
            amount=Decimal("666.67"),
            subscription_component=components[1],
        ),
    ]
    first = approve_payment_refund_case(
        club_id=club.id,
        case_id=first_case.id,
        actor_user_id=owner_user.id,
        idempotency_key="partial-round-1",
        amount=Decimal("333.33"),
        refund_kind=PaymentRefund.Kind.PARTIAL,
        reason="First partial",
    )
    second_event = BankPaymentProviderEvent.objects.create(
        club=club,
        order=order,
        provider=order.provider,
        event_type="acquiringInternetPayment",
        provider_event_id="partial-2",
        provider_payment_link_id=order.provider_payment_link_id,
        provider_status="REFUNDED_PARTIALLY",
        received_at=timezone.now() + timedelta(minutes=1),
        processing_status=BankPaymentProviderEvent.ProcessingStatus.FAILED,
    )
    second_case = PaymentRefundCase.objects.create(
        club=club,
        order=order,
        provider_event=second_event,
        refund_kind=PaymentRefund.Kind.PARTIAL,
        status=PaymentRefundCase.Status.RECONCILIATION_REQUIRED,
    )
    second = approve_payment_refund_case(
        club_id=club.id,
        case_id=second_case.id,
        actor_user_id=owner_user.id,
        idempotency_key="partial-round-2",
        amount=Decimal("333.33"),
        refund_kind=PaymentRefund.Kind.PARTIAL,
        reason="Second partial",
    )
    final_event = BankPaymentProviderEvent.objects.create(
        club=club,
        order=order,
        provider=order.provider,
        event_type="acquiringInternetPayment",
        provider_event_id="partial-3",
        provider_payment_link_id=order.provider_payment_link_id,
        provider_status="REFUNDED_PARTIALLY",
        received_at=timezone.now() + timedelta(minutes=2),
        processing_status=BankPaymentProviderEvent.ProcessingStatus.FAILED,
    )
    final_case = PaymentRefundCase.objects.create(
        club=club,
        order=order,
        provider_event=final_event,
        refund_kind=PaymentRefund.Kind.PARTIAL,
        status=PaymentRefundCase.Status.RECONCILIATION_REQUIRED,
    )

    with pytest.raises(BusinessLogicError) as exc_info:
        approve_payment_refund_case(
            club_id=club.id,
            case_id=final_case.id,
            actor_user_id=owner_user.id,
            idempotency_key="partial-round-invalid-final",
            amount=Decimal("333.34"),
            refund_kind=PaymentRefund.Kind.PARTIAL,
            reason="Must become full",
        )
    assert exc_info.value.code == "payment_refund_full_action_required"

    final = approve_payment_refund_case(
        club_id=club.id,
        case_id=final_case.id,
        actor_user_id=owner_user.id,
        idempotency_key="partial-round-final",
        amount=Decimal("333.34"),
        refund_kind=PaymentRefund.Kind.FULL,
        entitlement_action=PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
        legacy_enrollment_action="leave_unlinked",
        reason="Cumulative full refund",
    )

    assert first.amount + second.amount + final.amount == order.payment.amount
    for earning in earnings:
        debit = -sum(
            TrainerEarningAdjustment.objects.filter(
                source_earning=earning,
                kind=TrainerEarningAdjustment.Kind.REFUND,
            ).values_list("payable_amount_delta", flat=True),
            Decimal("0.00"),
        )
        assert debit == earning.amount


@pytest.mark.django_db
def test_closed_payroll_posts_refund_immediately_then_requires_explicit_open_date(
    club,
    owner_user,
):
    received_at = timezone.now()
    order, case = _refund_case(
        club=club,
        owner_user=owner_user,
        kind=PaymentRefund.Kind.PARTIAL,
        received_at=received_at,
    )
    trainer = TrainerFactory(club=club)
    earning = _sale_earning(
        club=club,
        payment=order.payment,
        trainer=trainer,
        amount=Decimal("1000.00"),
    )
    accounting_date = timezone.localdate(received_at)
    TrainerPayrollPeriodClose.objects.create(
        club=club,
        period_start=accounting_date,
        period_end=accounting_date,
        closed_by=owner_user,
        reason="Paid already",
    )

    refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=case.id,
        actor_user_id=owner_user.id,
        idempotency_key="closed-payroll-refund",
        amount=Decimal("1000.00"),
        refund_kind=PaymentRefund.Kind.PARTIAL,
        reason="Provider refund",
    )

    assert refund.status == PaymentRefund.Status.PAYROLL_ACTION_REQUIRED
    assert not TrainerEarningAdjustment.objects.filter(source_refund=refund).exists()
    assert get_pnl_report(
        club=club,
        date_from=accounting_date,
        date_to=accounting_date,
    )["refunded_income"] == Decimal("1000.00")

    open_date = accounting_date + timedelta(days=1)
    completed = complete_payment_refund_payroll(
        club_id=club.id,
        refund_id=refund.id,
        actor_user_id=owner_user.id,
        effective_date=open_date,
    )
    replayed = complete_payment_refund_payroll(
        club_id=club.id,
        refund_id=refund.id,
        actor_user_id=owner_user.id,
        effective_date=open_date,
    )

    adjustment = TrainerEarningAdjustment.objects.get(
        source_refund=refund,
        source_earning=earning,
    )
    assert completed.status == PaymentRefund.Status.COMPLETED
    assert replayed.id == completed.id
    assert adjustment.effective_date == open_date
    assert adjustment.payable_amount_delta == Decimal("-200.00")
    assert list(
        get_trainer_payroll_adjustments(
            club=club,
            trainer_id=trainer.id,
            date_from=open_date,
            date_to=open_date,
        )
    ) == [adjustment]


@pytest.mark.django_db
def test_later_refund_waits_for_prior_payroll_action_before_posting_debits(
    club,
    owner_user,
):
    received_at = timezone.now()
    order, first_case = _refund_case(
        club=club,
        owner_user=owner_user,
        kind=PaymentRefund.Kind.PARTIAL,
        received_at=received_at,
    )
    trainer = TrainerFactory(club=club)
    earning = _sale_earning(
        club=club,
        payment=order.payment,
        trainer=trainer,
        amount=Decimal("1000.00"),
    )
    first_accounting_date = timezone.localdate(received_at)
    open_date = first_accounting_date + timedelta(days=1)
    TrainerPayrollPeriodClose.objects.create(
        club=club,
        period_start=first_accounting_date,
        period_end=first_accounting_date,
        closed_by=owner_user,
        reason="Paid already",
    )
    second_event = BankPaymentProviderEvent.objects.create(
        club=club,
        order=order,
        provider=order.provider,
        event_type="acquiringInternetPayment",
        provider_event_id="refund-event-after-pending-payroll",
        provider_payment_link_id=order.provider_payment_link_id,
        provider_status="REFUNDED_PARTIALLY",
        amount_snapshot=Decimal("1000.00"),
        received_at=received_at + timedelta(days=1),
        processing_status=BankPaymentProviderEvent.ProcessingStatus.FAILED,
    )
    second_case = PaymentRefundCase.objects.create(
        club=club,
        order=order,
        provider_event=second_event,
        refund_kind=PaymentRefundCase.Kind.PARTIAL,
        detected_amount=Decimal("1000.00"),
        provider_refunded_at=second_event.received_at,
        status=PaymentRefundCase.Status.RECONCILIATION_REQUIRED,
    )
    first_refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=first_case.id,
        actor_user_id=owner_user.id,
        idempotency_key="first-refund-pending-payroll",
        amount=Decimal("1000.00"),
        refund_kind=PaymentRefund.Kind.PARTIAL,
        reason="First provider refund",
    )
    assert first_refund.status == PaymentRefund.Status.PAYROLL_ACTION_REQUIRED

    with pytest.raises(BusinessLogicError) as exc:
        approve_payment_refund_case(
            club_id=club.id,
            case_id=second_case.id,
            actor_user_id=owner_user.id,
            idempotency_key="second-refund-blocked-by-payroll",
            amount=Decimal("1000.00"),
            refund_kind=PaymentRefund.Kind.PARTIAL,
            reason="Second provider refund",
        )

    assert exc.value.code == "payment_refund_prior_payroll_action_required"
    assert PaymentRefund.objects.for_club(club).filter(payment=order.payment).count() == 1
    second_case.refresh_from_db()
    assert second_case.status == PaymentRefundCase.Status.RECONCILIATION_REQUIRED

    complete_payment_refund_payroll(
        club_id=club.id,
        refund_id=first_refund.id,
        actor_user_id=owner_user.id,
        effective_date=open_date,
    )
    second_refund = approve_payment_refund_case(
        club_id=club.id,
        case_id=second_case.id,
        actor_user_id=owner_user.id,
        idempotency_key="second-refund-blocked-by-payroll",
        amount=Decimal("1000.00"),
        refund_kind=PaymentRefund.Kind.PARTIAL,
        reason="Second provider refund",
    )

    first_debit = TrainerEarningAdjustment.objects.get(
        source_refund=first_refund,
        source_earning=earning,
    )
    second_debit = TrainerEarningAdjustment.objects.get(
        source_refund=second_refund,
        source_earning=earning,
    )
    assert first_debit.payable_amount_delta == Decimal("-200.00")
    assert second_debit.payable_amount_delta == Decimal("-200.00")
    assert first_debit.effective_date == open_date
    assert second_debit.effective_date == open_date


@pytest.mark.django_db
def test_refund_rejects_foreign_case_unconfirmed_payment_and_over_refund(
    club,
    other_club,
    owner_user,
):
    order, case = _refund_case(club=club, owner_user=owner_user)

    with pytest.raises(PaymentRefundCase.DoesNotExist):
        approve_payment_refund_case(
            club_id=other_club.id,
            case_id=case.id,
            actor_user_id=owner_user.id,
            idempotency_key="foreign-refund",
            amount=order.payment.amount,
            refund_kind=PaymentRefund.Kind.FULL,
            entitlement_action=PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
            reason="Foreign",
        )

    order.payment.status = Payment.Status.PENDING
    order.payment.save(update_fields=["status", "updated_at"])
    with pytest.raises(BusinessLogicError) as exc_info:
        approve_payment_refund_case(
            club_id=club.id,
            case_id=case.id,
            actor_user_id=owner_user.id,
            idempotency_key="pending-refund",
            amount=order.payment.amount,
            refund_kind=PaymentRefund.Kind.FULL,
            entitlement_action=PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
            reason="Pending",
        )
    assert exc_info.value.code == "payment_refund_payment_not_confirmed"

    order.payment.status = Payment.Status.CONFIRMED
    order.payment.save(update_fields=["status", "updated_at"])
    with pytest.raises(BusinessLogicError) as exc_info:
        approve_payment_refund_case(
            club_id=club.id,
            case_id=case.id,
            actor_user_id=owner_user.id,
            idempotency_key="over-refund",
            amount=order.payment.amount + Decimal("0.01"),
            refund_kind=PaymentRefund.Kind.FULL,
            entitlement_action=PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
            reason="Over",
        )
    assert exc_info.value.code == "payment_refund_amount_exceeds_payment"


@pytest.mark.django_db
@pytest.mark.parametrize("invalid_amount", ["", "not-money", "NaN", "Infinity"])
def test_refund_rejects_invalid_amount_without_resolving_case(
    club,
    owner_user,
    invalid_amount,
):
    _order, case = _refund_case(club=club, owner_user=owner_user)

    with pytest.raises(BusinessLogicError) as exc_info:
        approve_payment_refund_case(
            club_id=club.id,
            case_id=case.id,
            actor_user_id=owner_user.id,
            idempotency_key=f"invalid-amount-{invalid_amount}",
            amount=invalid_amount,
            refund_kind=PaymentRefund.Kind.FULL,
            entitlement_action=PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
            reason="Invalid amount must not mutate accounting",
        )

    case.refresh_from_db()
    assert exc_info.value.code == "payment_refund_amount_invalid"
    assert case.status == PaymentRefundCase.Status.DETECTED
    assert not PaymentRefund.objects.filter(refund_case=case).exists()


@pytest.mark.django_db
def test_detected_full_refund_case_cannot_be_downgraded_to_partial(
    club,
    owner_user,
):
    _order, case = _refund_case(club=club, owner_user=owner_user)

    with pytest.raises(BusinessLogicError) as exc_info:
        approve_payment_refund_case(
            club_id=club.id,
            case_id=case.id,
            actor_user_id=owner_user.id,
            idempotency_key="full-case-downgrade",
            amount=Decimal("1000.00"),
            refund_kind=PaymentRefund.Kind.PARTIAL,
            reason="Must preserve provider full-refund evidence",
        )

    case.refresh_from_db()
    assert exc_info.value.code == "payment_refund_case_kind_mismatch"
    assert case.status == PaymentRefundCase.Status.DETECTED
    assert not PaymentRefund.objects.filter(refund_case=case).exists()


@pytest.mark.django_db
@pytest.mark.parametrize("source", ["manual", "provider"])
def test_owner_revoke_preserves_fully_refunded_renewal_with_explicit_keep(club, owner_user, settings, source):
    from apps.billing.refund_services import record_manual_payment_refund
    from apps.billing.service_modules.subscription_balance_audit import subscription_balance_findings

    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = True
    owner_case = renewal_case = None
    if source == "provider":
        owner_order, owner_case = _refund_case(club=club, owner_user=owner_user)
        owner_payment = owner_order.payment
    else:
        tariff = TariffFactory(
            club=club, training_type=TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP),
            trainer_payout_policy=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        subscription = SubscriptionFactory(
            club=club, student=StudentFactory(club=club), tariff=tariff, paid_amount=Decimal("1000"),
        )
        SubscriptionComponentFactory(
            club=club, subscription=subscription, trainer_payout_policy_snapshot=Tariff.PayoutPolicy.ON_CHECKIN,
        )
        owner_payment = PaymentFactory(
            club=club, student=subscription.student, tariff=tariff, subscription=subscription,
            amount=Decimal("1000"), original_amount=Decimal("1000"),
            payment_method=Payment.Method.CASH, status=Payment.Status.CONFIRMED, verified_at=timezone.now(),
        )
    membership = _attach_group_membership_to_refund_payment(payment=owner_payment)
    TrainingGroupRolloutStateFactory(club=club)
    renewal = _renewal_payment_for_group_membership(
        club=club, owner_user=owner_user, owner_payment=owner_payment, membership=membership,
    )
    if source == "provider":
        _, renewal_case = _refund_case_for_existing_payment(
            club=club, owner_user=owner_user, payment=renewal, provider_event_id="kept-renewal",
        )
    else:
        renewal.payment_method = Payment.Method.CASH
        renewal.save(update_fields=["payment_method"])

    def refund(payment, action, key, case=None):
        if source == "manual":
            return record_manual_payment_refund(
                club_id=club.id, actor_user_id=owner_user.id, payment_id=payment.id,
                subscription_id=payment.subscription_id, amount=payment.amount,
                accounting_date=timezone.localdate(), reason="Explicit refund policy", idempotency_key=key,
                entitlement_action=action,
            )
        return approve_payment_refund_case(
            club_id=club.id, actor_user_id=owner_user.id, case_id=case.id,
            amount=payment.amount, refund_kind=PaymentRefund.Kind.FULL, reason="Explicit refund policy",
            idempotency_key=key, entitlement_action=action,
        )

    refund(renewal, PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS, "keep-renewal", renewal_case)
    result = refund(owner_payment, PaymentRefund.EntitlementDisposition.REVOKE_REMAINING, "revoke-owner", owner_case)
    membership.refresh_from_db()
    renewal.subscription.refresh_from_db()
    owner_payment.subscription.refresh_from_db()
    assert renewal.subscription.status == Subscription.Status.ACTIVE
    assert membership.status == TrainingGroupMembership.Status.ACTIVE
    assert result.enrollment_disposition == PaymentRefund.EnrollmentDisposition.KEPT
    assert subscription_balance_findings(subscription=owner_payment.subscription) == []
