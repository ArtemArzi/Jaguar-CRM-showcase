from __future__ import annotations

import datetime as dt
from datetime import date, datetime
from decimal import Decimal
from typing import Literal
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ObjectDoesNotExist
from django.utils import timezone
from ninja import Schema
from pydantic import ConfigDict, Field


class TrainingTypeIn(Schema):
    name: str
    slug: str
    kind: Literal["group", "personal", "mini_group"] = "group"
    grade_system_id: int | None = None
    drop_in_price: Decimal | None = None


class TrainingTypeOut(Schema):
    id: int
    name: str
    slug: str
    kind: str
    is_active: bool
    grade_system_id: int | None
    drop_in_price: Decimal | None


class TariffIn(Schema):
    name: str
    training_type_id: int
    price: Decimal
    trainings_limit: int | None = None
    duration_days: int
    scope: str = "club"
    location_id: int | None = None
    description: str = ""
    trainer_payout_policy: Literal["none", "on_checkin", "on_payment"] | None = None
    components: list[TariffComponentIn] | None = None
    personal_booking_trainer_id: int | None = None
    is_personal_booking_default: bool = False


class TariffComponentIn(Schema):
    name: str = ""
    training_type_id: int
    entitlement_kind: Literal["finite_credits", "weekly_limit", "unlimited"] = "finite_credits"
    credits_total: int | None = None
    weekly_limit: int | None = None
    scope: str = "club"
    location_id: int | None = None
    trainer_payout_policy: Literal["none", "on_checkin", "on_payment"] | None = None
    paid_amount_basis: Decimal


class TariffComponentOut(Schema):
    id: int
    name: str
    training_type_id: int
    entitlement_kind: str
    credits_total: int | None
    weekly_limit: int | None
    scope: str
    location_id: int | None
    trainer_payout_policy: str
    paid_amount_basis: Decimal
    sort_order: int


class TariffOut(Schema):
    id: int
    name: str
    training_type: TrainingTypeOut
    price: Decimal
    trainings_limit: int | None
    duration_days: int
    scope: str
    location_id: int | None
    is_active: bool
    is_personal_booking_default: bool
    personal_booking_trainer_id: int | None
    description: str
    payout_timing_hint: str = ""
    requires_package_owner: bool = False
    trainer_payout_policy: str = ""
    components: list[TariffComponentOut] = []

    @staticmethod
    def _active_components(obj) -> list:
        components = getattr(obj, "active_components", None)
        if components is None:
            try:
                components = list(
                    obj.components.filter(is_active=True)
                    .select_related("training_type", "location")
                    .order_by("sort_order", "id")
                )
            except (AttributeError, ObjectDoesNotExist):
                components = []
        return components

    @staticmethod
    def _default_policy(obj) -> str:
        policy = getattr(obj, "trainer_payout_policy", "") or ""
        if policy:
            return policy
        kind = obj.training_type.kind
        if kind == "group":
            return "on_payment"
        if kind in {"personal", "mini_group"}:
            return "on_checkin"
        return "none"

    @staticmethod
    def resolve_payout_timing_hint(obj) -> str:
        components = TariffOut._active_components(obj)
        if components:
            hints = {
                {
                    "on_payment": "after_payment_confirmation",
                    "on_checkin": "per_checkin",
                    "none": "no_trainer_payout",
                }.get(component.trainer_payout_policy, "")
                for component in components
            }
            hints.discard("")
            if len(hints) > 1:
                return "mixed"
            if hints:
                return hints.pop()

        policy = TariffOut._default_policy(obj)
        return {
            "on_payment": "after_payment_confirmation",
            "on_checkin": "per_checkin",
            "none": "no_trainer_payout",
        }.get(policy, "")

    @staticmethod
    def resolve_requires_package_owner(obj) -> bool:
        components = TariffOut._active_components(obj)
        if components:
            return any(
                component.training_type.kind in {"personal", "mini_group"}
                for component in components
            )
        return obj.training_type.kind in {"personal", "mini_group"}

    @staticmethod
    def resolve_trainer_payout_policy(obj) -> str:
        if not getattr(obj, "_expose_internal_contract", False):
            return ""
        return TariffOut._default_policy(obj)

    @staticmethod
    def resolve_components(obj) -> list:
        if not getattr(obj, "_expose_internal_contract", False):
            return []
        return TariffOut._active_components(obj)


class TariffUpdate(Schema):
    name: str | None = None
    price: Decimal | None = None
    trainings_limit: int | None = None
    duration_days: int | None = None
    is_active: bool | None = None
    description: str | None = None
    trainer_payout_policy: Literal["none", "on_checkin", "on_payment"] | None = None
    components: list[TariffComponentIn] | None = None
    personal_booking_trainer_id: int | None = None
    is_personal_booking_default: bool | None = None


class SubscriptionIn(Schema):
    student_id: int
    tariff_id: int
    payment_method: Literal["cash", "transfer"]
    package_owner_trainer_id: int | None = None
    debt_ids: list[int] = []


class SubscriptionBookingEntitlementOut(Schema):
    training_type_id: int
    training_type_name: str
    training_type_kind: str
    credits_left: int | None
    weekly_limit: int | None
    weekly_used: int | None = None
    scope: str
    location_id: int | None


class SubscriptionOut(Schema):
    id: int
    student_id: int
    tariff: TariffOut
    paid_amount: Decimal
    status: str
    trainings_left: int | None
    trainings_used: int
    activated_at: datetime | None
    expires_at: datetime | None
    scope: str
    location_id: int | None
    training_type_kind: str = ""
    package_owner_trainer_id: int | None = None
    package_owner_trainer_name: str = ""
    freeze_status: str | None = None
    has_components: bool = False
    booking_entitlements: list[SubscriptionBookingEntitlementOut] = []
    booking_date: date | None = None
    renewal_target_tariff_id: int | None = None
    renewal_target_tariff_name: str = ""
    renewal_target_price: Decimal | None = None

    @staticmethod
    def resolve_paid_amount(obj):
        paid_amount = getattr(obj, "paid_amount", None)
        if paid_amount is not None:
            return paid_amount
        try:
            return obj.payment.amount
        except ObjectDoesNotExist:
            return obj.tariff.price

    @staticmethod
    def resolve_training_type_kind(obj) -> str:
        return obj.tariff.training_type.kind

    @staticmethod
    def _booking_components(obj) -> list:
        components = getattr(obj, "active_booking_components", None)
        if components is not None:
            return [
                component
                for component in components
                if component.credits_left is None or component.credits_left > 0
            ]
        return []

    @staticmethod
    def resolve_has_components(obj) -> bool:
        components = getattr(obj, "subscription_component_presence", None)
        return bool(components)

    @staticmethod
    def resolve_booking_entitlements(obj) -> list[dict]:
        return [
            {
                "training_type_id": component.training_type_id,
                "training_type_name": component.training_type.name,
                "training_type_kind": component.training_type.kind,
                "credits_left": component.credits_left,
                "weekly_limit": component.weekly_limit,
                "weekly_used": getattr(component, "booking_week_used", None),
                "scope": component.scope,
                "location_id": component.location_id,
            }
            for component in SubscriptionOut._booking_components(obj)
        ]

    @staticmethod
    def resolve_booking_date(obj) -> date | None:
        return getattr(obj, "booking_date_context", None)

    @staticmethod
    def _active_package_allocation(obj):
        allocations = getattr(obj, "active_trainer_allocations", None)
        if allocations is not None:
            return allocations[0] if allocations else None
        return (
            obj.trainer_allocations.filter(club_id=obj.club_id, is_active=True)
            .select_related("owner_trainer")
            .first()
        )

    @staticmethod
    def resolve_package_owner_trainer_id(obj) -> int | None:
        allocation = SubscriptionOut._active_package_allocation(obj)
        return allocation.owner_trainer_id if allocation else None

    @staticmethod
    def resolve_package_owner_trainer_name(obj) -> str:
        allocation = SubscriptionOut._active_package_allocation(obj)
        return str(allocation.owner_trainer) if allocation else ""

    @staticmethod
    def resolve_freeze_status(obj) -> str | None:
        pending_freezes = getattr(obj, "pending_freezes", None)
        if pending_freezes is not None:
            return "pending" if pending_freezes else None
        return "pending" if obj.freezes.filter(status="pending").exists() else None

    @staticmethod
    def _renewal_offer(obj):
        offer = getattr(obj, "renewal_offer", None)
        if offer is not None:
            return offer
        from apps.billing.service_modules.renewals import get_renewal_offer

        offer = get_renewal_offer(club_id=obj.club_id, source_tariff=obj.tariff)
        obj.renewal_offer = offer
        return offer

    @staticmethod
    def resolve_renewal_target_tariff_id(obj) -> int | None:
        offer = SubscriptionOut._renewal_offer(obj)
        return offer.target_tariff_id if offer.is_available else None

    @staticmethod
    def resolve_renewal_target_tariff_name(obj) -> str:
        offer = SubscriptionOut._renewal_offer(obj)
        return offer.target_tariff_name if offer.is_available else ""

    @staticmethod
    def resolve_renewal_target_price(obj) -> Decimal | None:
        offer = SubscriptionOut._renewal_offer(obj)
        return offer.target_price if offer.is_available else None


# --- Payment ---


class PaymentIn(Schema):
    student_id: int
    tariff_id: int
    payment_method: Literal["cash", "transfer"]
    discount_ids: list[int] = []
    debt_ids: list[int] = []
    # GROUP: seller for sale earning. PERSONAL/MINI: sale recorder can differ from package owner.
    seller_trainer_id: int | None = None
    package_owner_trainer_id: int | None = None
    # GROUP conversion target. Seller attribution is resolved from this schedule server-side.
    target_training_group_id: int | None = None
    target_schedule_id: int | None = None
    target_start_date: date | None = None
    renewed_from_subscription_id: int | None = None
    idempotency_key: str | None = None
    expected_target_tariff_id: int | None = None
    expected_target_price: Decimal | None = None
    # NOTE: NO amount field -- anti-fraud


class GroupSaleManualV2In(Schema):
    """Frozen strict v2 manual group-sale command; Slice 4 owns its route."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["v2"]
    student_id: int
    tariff_id: int
    payment_method: Literal["cash", "transfer"]
    target_training_group_id: int
    target_schedule_id: int
    target_start_date: date
    expected_offer_digest: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=120)


class GroupSaleBankOrderV2In(Schema):
    """Frozen strict v2 SBP group-sale command; no authoritative amount field."""

    model_config = ConfigDict(extra="forbid")

    protocol_version: Literal["v2"]
    student_id: int
    tariff_id: int
    target_training_group_id: int
    target_schedule_id: int
    target_start_date: date
    expected_offer_digest: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=120)
    buyer_email: str | None = None


class GroupSaleOfferWeeklyScheduleOut(Schema):
    schedule_id: int
    updated_at: datetime
    day_of_week: int
    start_time: dt.time
    end_time: dt.time
    trainer_id: int
    trainer_name: str
    location_id: int
    location_name: str


class GroupSaleOfferStudentOut(Schema):
    id: int
    display_name: str


class GroupSaleOfferTariffOut(Schema):
    id: int
    name: str
    price: Decimal
    trainings_limit: int | None = None
    duration_days: int | None = None


class GroupSaleOfferGroupOut(Schema):
    id: int
    name: str
    responsible_trainer_id: int
    responsible_trainer_name: str
    location_id: int
    location_name: str
    weekly_schedule: list[GroupSaleOfferWeeklyScheduleOut]


class GroupSaleOfferOccurrenceOut(Schema):
    schedule_id: int
    date: date
    start_time: dt.time
    end_time: dt.time
    trainer_id: int
    trainer_name: str
    location_id: int
    location_name: str
    is_rescheduled: bool
    is_substitute: bool


class GroupSaleOfferPreviewOut(Schema):
    """Complete, signed commercial display terms for one v2 group command."""

    protocol_version: Literal["v2"]
    student: GroupSaleOfferStudentOut
    tariff: GroupSaleOfferTariffOut
    group: GroupSaleOfferGroupOut
    selected_occurrence: GroupSaleOfferOccurrenceOut
    expected_action: Literal["new_admission"]
    buyer_email_required: bool
    offer_digest: str


class GroupSaleCommandOut(Schema):
    payment_id: int
    subscription_id: int
    bank_payment_order_id: int | None = None
    workspace_state: Literal["lead", "student"]
    finance_state: Literal[
        "pending_manual",
        "provider_pending",
        "confirmed",
        "rejected",
        "cancelled",
        "expired",
        "failed",
    ]
    payment_status: str
    bank_payment_order_status: str = ""
    fulfillment_state: str = ""
    provider_payment_url: str = ""
    allowed_actions: list[str] = []
    command_replayed: bool = False


class ManualSubscriptionRenewalIn(Schema):
    # This typed boundary deliberately rejects extra commercial terms.  The
    # source subscription is the only tariff/amount/debt authority.
    model_config = ConfigDict(extra="forbid")

    student_id: int
    renewed_from_subscription_id: int
    payment_method: Literal["cash", "transfer"]
    idempotency_key: str
    discount_ids: list[int] = []
    expected_target_tariff_id: int | None = None
    expected_target_price: Decimal | None = None


class GroupEnrollmentOptionOut(Schema):
    schedule_id: int
    group_name: str
    trainer_id: int
    trainer_name: str
    location_id: int
    location_name: str
    training_type_id: int
    training_type_name: str
    day_of_week: int
    start_time: dt.time
    end_time: dt.time
    next_occurrence_date: date
    occurrence_dates: list[date]
    is_latest_trial_group: bool = False


    training_group_id: int | None = None
    responsible_trainer_id: int | None = None
    responsible_trainer_name: str = ""
    target_group_membership_id: int | None = None
    # A canonical card never lets the client guess a renewal source.  A
    # renewal with a null source is deliberately non-actionable.
    group_membership_action: Literal["new_admission", "renewal"] = "new_admission"
    renewed_from_subscription_id: int | None = None
    slot_schedule_ids: list[int] = []
    weekly_schedule: list[dict] = []
    upcoming_occurrences: list[dict] = []
    is_canonical_group_card: bool = False


class PaymentCapabilitiesOut(Schema):
    online_payments_enabled: bool
    payment_mode: Literal["sbp"] = "sbp"
    payment_modes: list[Literal["sbp"]] = ["sbp"]
    payment_creation_enabled: bool
    payment_reconciliation_enabled: bool
    payment_unavailable_reason: str = ""
    can_create_payment_order: bool
    can_request_payment_reconciliation: bool
    training_group_rollout_mode: str
    training_group_payment_selection_mode: Literal["legacy", "canonical", "disabled"]
    canonical_group_selection_enabled: bool


class PaymentOut(Schema):
    id: int
    student_id: int
    tariff: TariffOut
    amount: Decimal
    original_amount: Decimal
    payment_method: str
    status: str
    recorded_by_id: int
    verified_by_id: int | None
    verified_at: datetime | None
    created_at: datetime
    seller_trainer_id: int | None = None
    seller_trainer_name: str = ""
    training_type_kind: str = ""
    package_owner_trainer_id: int | None = None
    package_owner_trainer_name: str = ""
    target_schedule_id: int | None = None
    target_start_date: date | None = None
    target_group_name_snapshot: str = ""
    target_location_id_snapshot: int | None = None
    target_location_name_snapshot: str = ""
    target_trainer_id_snapshot: int | None = None
    target_trainer_name_snapshot: str = ""
    target_training_type_id_snapshot: int | None = None
    target_training_type_kind_snapshot: str = ""
    sale_trainer_id_snapshot: int | None = None
    sale_trainer_name_snapshot: str = ""
    sale_attribution_source: str = ""
    discount_ids: list[int] = []
    conversion_enrollment_id: int | None = None
    target_training_group_id: int | None = None
    target_group_membership_id: int | None = None
    conversion_group_membership_id: int | None = None
    group_membership_action_snapshot: str = ""
    renewed_from_subscription_id: int | None = None
    renewal_source_tariff_name_snapshot: str = ""
    command_replayed: bool = False

    @staticmethod
    def resolve_discount_ids(obj):
        discounts = getattr(obj, "prefetched_applied_discounts", None)
        if discounts is not None:
            return [discount.id for discount in discounts]
        return list(obj.applied_discounts.values_list("id", flat=True))

    @staticmethod
    def resolve_seller_trainer_name(obj) -> str:
        return str(obj.seller_trainer) if getattr(obj, "seller_trainer", None) else ""

    @staticmethod
    def resolve_training_type_kind(obj) -> str:
        return obj.tariff.training_type.kind

    @staticmethod
    def _active_package_allocation(obj):
        subscription = getattr(obj, "subscription", None)
        if subscription is None:
            return None
        return SubscriptionOut._active_package_allocation(subscription)

    @staticmethod
    def resolve_package_owner_trainer_id(obj) -> int | None:
        if getattr(obj, "package_owner_trainer_id", None):
            return obj.package_owner_trainer_id
        allocation = PaymentOut._active_package_allocation(obj)
        return allocation.owner_trainer_id if allocation else None

    @staticmethod
    def resolve_package_owner_trainer_name(obj) -> str:
        if getattr(obj, "package_owner_trainer", None):
            return str(obj.package_owner_trainer)
        allocation = PaymentOut._active_package_allocation(obj)
        return str(allocation.owner_trainer) if allocation else ""

    @staticmethod
    def resolve_sale_trainer_name_snapshot(obj) -> str:
        sale_trainer_id = getattr(obj, "sale_trainer_id_snapshot", None)
        seller_trainer = getattr(obj, "seller_trainer", None)
        if sale_trainer_id and seller_trainer and seller_trainer.id == sale_trainer_id:
            return str(obj.seller_trainer)
        target_group = getattr(obj, "target_training_group", None)
        responsible_trainer = (
            getattr(target_group, "responsible_trainer", None)
            if target_group is not None
            else None
        )
        if (
            sale_trainer_id
            and responsible_trainer
            and responsible_trainer.id == sale_trainer_id
        ):
            return str(responsible_trainer)
        if (
            sale_trainer_id
            and getattr(obj, "target_trainer_id_snapshot", None) == sale_trainer_id
        ):
            return getattr(obj, "target_trainer_name_snapshot", "")
        return ""

    @staticmethod
    def resolve_renewed_from_subscription_id(obj) -> int | None:
        return obj.subscription.renewed_from_id if getattr(obj, "subscription", None) else None

    @staticmethod
    def resolve_command_replayed(obj) -> bool:
        return bool(getattr(obj, "_command_replayed", False))


class PaymentVerifyIn(Schema):
    action: str  # "confirm" | "reject"
    rejection_reason: str = ""


class BankPaymentOrderCreateIn(Schema):
    student_id: int
    tariff_id: int | None = None
    discount_ids: list[int] = []
    debt_ids: list[int] = []
    seller_trainer_id: int | None = None
    package_owner_trainer_id: int | None = None
    target_schedule_id: int | None = None
    target_start_date: date | None = None
    buyer_email: str | None = None
    buyer_phone: str | None = None

    target_training_group_id: int | None = None
    renewed_from_subscription_id: int | None = None
    idempotency_key: str | None = None
    expected_target_tariff_id: int | None = None
    expected_target_price: Decimal | None = None

class BankPaymentOrderReviewIn(Schema):
    resolution: Literal["confirm_paid", "reject", "mark_refunded", "mark_refunded_partially"]
    reason: str = ""
    evidence: dict = Field(default_factory=dict)


class PaymentRefundCaseOut(Schema):
    id: int
    order_id: int
    payment_id: int
    student_id: int
    refund_kind: str
    detected_amount: Decimal | None
    provider_refunded_at: datetime | None
    status: str
    provider_event_id: int | None
    legacy_review_event_id: int | None

    @staticmethod
    def resolve_payment_id(obj) -> int:
        return obj.order.payment_id

    @staticmethod
    def resolve_student_id(obj) -> int:
        return obj.order.student_id


class PaymentRefundApproveIn(Schema):
    idempotency_key: str
    amount: Decimal
    refund_kind: Literal["full", "partial"]
    reason: str
    entitlement_action: Literal["keep_club_absorbs", "revoke_remaining"] | None = None
    legacy_enrollment_action: Literal["leave_unlinked", "cancel_selected"] | None = None
    legacy_enrollment_id: int | None = None


class PaymentRefundPayrollIn(Schema):
    effective_date: date


class PaymentRefundOut(Schema):
    id: int
    refund_case_id: int
    order_id: int
    payment_id: int
    subscription_id: int
    amount: Decimal
    currency: str
    refund_kind: str
    provider_refunded_at: datetime | None
    accounting_date: date
    entitlement_disposition: str
    enrollment_disposition: str
    personal_booking_disposition: str
    settled_debt_disposition: str
    status: str
    payroll_effective_date: date | None


class SelfServiceBankPaymentOrderCreateIn(Schema):
    tariff_id: int | None = None
    debt_ids: list[int] = []
    buyer_email: str | None = None
    buyer_phone: str | None = None
    renewed_from_subscription_id: int | None = None
    idempotency_key: str | None = None
    expected_target_tariff_id: int | None = None
    expected_target_price: Decimal | None = None


class BankPaymentOrderOut(Schema):
    id: int
    payment_id: int
    subscription_id: int
    student_id: int
    tariff_id: int
    target_schedule_id: int | None = None
    target_start_date: date | None = None
    debt_ids: list[int] = []
    target_training_group_id: int | None = None
    target_group_membership_id: int | None = None
    conversion_group_membership_id: int | None = None
    group_membership_action_snapshot: str = ""
    provider: str
    source: str
    status: str
    amount_snapshot: Decimal
    currency: str
    purpose_snapshot: str
    provider_payment_link_id: str
    provider_payment_url: str
    provider_payment_modes: list[str]
    provider_status: str
    expires_at: datetime
    paid_at: datetime | None
    receipt_mode: str
    receipt_status: str
    receipt_url: str
    renewed_from_subscription_id: int | None = None
    payment_status: str = ""
    subscription_status: str = ""
    is_renewal: bool = False
    intent_kind: str = "subscription"
    personal_booking_reservation_id: int | None = None
    personal_drop_in_booking_id: int | None = None
    fulfillment_state: str = ""
    can_pay: bool = False
    can_share: bool = False
    can_copy: bool = False
    can_show_qr: bool = False
    can_request_refresh: bool = False
    can_cancel: bool = False
    command_replayed: bool = False
    created_at: datetime
    renewal_source_tariff_id: int | None = None
    renewal_source_tariff_name: str = ""
    renewal_target_tariff_id: int | None = None
    renewal_target_tariff_name: str = ""
    renewal_target_price: Decimal | None = None

    @staticmethod
    def resolve_tariff_id(obj) -> int:
        return obj.payment.tariff_id

    @staticmethod
    def resolve_renewal_source_tariff_id(obj) -> int | None:
        source = getattr(obj, "renewed_from_subscription", None)
        return source.tariff_id if source is not None else None

    @staticmethod
    def resolve_renewal_source_tariff_name(obj) -> str:
        source = getattr(obj, "renewed_from_subscription", None)
        if source is None:
            return ""
        return getattr(obj.payment, "renewal_source_tariff_name_snapshot", "") or source.tariff.name

    @staticmethod
    def resolve_renewal_target_tariff_name(obj) -> str:
        return obj.payment.tariff.name if obj.renewed_from_subscription_id else ""

    @staticmethod
    def resolve_renewal_target_tariff_id(obj) -> int | None:
        return obj.payment.tariff_id if obj.renewed_from_subscription_id else None

    @staticmethod
    def resolve_renewal_target_price(obj) -> Decimal | None:
        return obj.payment.tariff.price if obj.renewed_from_subscription_id else None

    @staticmethod
    def resolve_target_schedule_id(obj) -> int | None:
        return obj.payment.target_schedule_id

    @staticmethod
    def resolve_target_start_date(obj) -> date | None:
        return obj.payment.target_start_date

    @staticmethod
    def resolve_target_training_group_id(obj) -> int | None:
        return obj.payment.target_training_group_id

    @staticmethod
    def resolve_target_group_membership_id(obj) -> int | None:
        return obj.payment.target_group_membership_id

    @staticmethod
    def resolve_conversion_group_membership_id(obj) -> int | None:
        return obj.payment.conversion_group_membership_id

    @staticmethod
    def resolve_group_membership_action_snapshot(obj) -> str:
        return obj.payment.group_membership_action_snapshot

    @staticmethod
    def resolve_debt_ids(obj) -> list[int]:
        debts = getattr(obj.payment, "open_settled_debts", None)
        if debts is not None:
            return [debt.id for debt in debts]
        return list(
            obj.payment.settled_debts.filter(resolved_at__isnull=True).values_list(
                "id",
                flat=True,
            )
        )

    @staticmethod
    def resolve_payment_status(obj) -> str:
        return obj.payment.status

    @staticmethod
    def resolve_subscription_status(obj) -> str:
        return obj.subscription.status

    @staticmethod
    def resolve_is_renewal(obj) -> bool:
        return obj.renewed_from_subscription_id is not None

    @staticmethod
    def resolve_command_replayed(obj) -> bool:
        return bool(getattr(obj, "_command_replayed", False))

    @staticmethod
    def resolve_intent_kind(obj) -> str:
        if obj.personal_booking_reservation_id_snapshot is not None:
            return "personal_booking"
        if obj.personal_drop_in_booking_id_snapshot is not None:
            return "personal_drop_in"
        if obj.renewed_from_subscription_id is not None:
            return "renewal"
        return "subscription"

    @staticmethod
    def resolve_personal_booking_reservation_id(obj) -> int | None:
        return obj.personal_booking_reservation_id_snapshot

    @staticmethod
    def resolve_personal_drop_in_booking_id(obj) -> int | None:
        return obj.personal_drop_in_booking_id_snapshot

    @staticmethod
    def resolve_fulfillment_state(obj) -> str:
        if obj.status == "approved":
            return "fulfilled" if obj.subscription.status == "active" else "fulfillment_pending"
        if obj.status == "manual_review":
            return "manual_review"
        if obj.status in {"failed", "expired", "cancelled"}:
            return "not_fulfilled"
        if obj.status in {"refunded", "refunded_partially"}:
            return obj.status
        return "pending_payment"

    @staticmethod
    def _can_use_payment_link(obj) -> bool:
        if obj.provider == "mock" and not (
            settings.DEBUG
            and settings.PAYMENT_PROVIDER == "mock"
            and settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED
        ):
            return False
        try:
            parsed_payment_url = urlparse(obj.provider_payment_url)
        except (TypeError, ValueError):
            return False
        has_safe_payment_url = bool(
            (
                parsed_payment_url.scheme == "https"
                or (
                    obj.provider == "mock"
                    and parsed_payment_url.scheme == "http"
                    and parsed_payment_url.hostname in {"localhost", "127.0.0.1", "::1"}
                )
            )
            and parsed_payment_url.hostname
            and not parsed_payment_url.username
            and not parsed_payment_url.password
        )
        return bool(
            getattr(obj, "payment_action_mode", "")
            and obj.status in {"created", "pending", "authorized"}
            and has_safe_payment_url
            and obj.expires_at > timezone.now()
        )

    @staticmethod
    def resolve_can_pay(obj) -> bool:
        return (
            getattr(obj, "payment_action_mode", "") == "self_service"
            and BankPaymentOrderOut._can_use_payment_link(obj)
        )

    @staticmethod
    def resolve_can_share(obj) -> bool:
        return getattr(obj, "payment_action_mode", "") == "staff" and BankPaymentOrderOut._can_use_payment_link(obj)

    @staticmethod
    def resolve_can_copy(obj) -> bool:
        return getattr(obj, "payment_action_mode", "") == "staff" and BankPaymentOrderOut._can_use_payment_link(obj)

    @staticmethod
    def resolve_can_show_qr(obj) -> bool:
        return getattr(obj, "payment_action_mode", "") == "staff" and BankPaymentOrderOut._can_use_payment_link(obj)

    @staticmethod
    def resolve_can_request_refresh(obj) -> bool:
        # This permits only a refetch of the actor-scoped projection.  It never
        # grants browser-triggered provider I/O; reconciliation remains server
        # cooldown/lease controlled.
        refreshable_status = obj.status in {
            "created",
            "pending",
            "authorized",
            "manual_review",
        } or (
            obj.status == "approved" and obj.subscription.status != "active"
        )
        return bool(
            getattr(obj, "can_refresh_source_allowed", True)
            and
            getattr(obj, "payment_action_mode", "")
            and refreshable_status
        )

    @staticmethod
    def resolve_can_cancel(obj) -> bool:
        if getattr(obj, "can_cancel_source_allowed", True) is False:
            return False
        if (
            obj.provider == "tochka"
            and obj.link_creation_state in {"claimed", "dispatched", "unknown"}
        ):
            return False
        return (
            obj.status in {"created", "pending", "authorized"}
            and obj.payment.status == "pending"
            and obj.subscription.status == "pending"
        )


class BankPaymentWebhookOut(Schema):
    event_id: int | None
    delivery_id: int
    order_id: int | None
    processing_status: str
    provider_status: str


class DeferredProviderEventReplayOut(Schema):
    processed: int
    failed: int
    ignored: int
    deferred: int
    skipped: int


class PaymentReturnExchangeIn(Schema):
    state: str
    browser_binding: str


# --- Discount ---


class DiscountIn(Schema):
    name: str
    discount_type: str  # "percent" | "fixed"
    value: Decimal


class DiscountOut(Schema):
    id: int
    name: str
    discount_type: str
    value: Decimal
    is_active: bool


class DiscountUpdate(Schema):
    name: str | None = None
    value: Decimal | None = None
    is_active: bool | None = None


# --- Debt ---


class DebtOut(Schema):
    id: int
    student_id: int
    student_name: str = ""
    checkin_id: int
    booking_id: int | None = None
    required_tariff_id: int | None = None
    tariff_price: Decimal | None
    reason: str
    resolution_type: str
    resolved_at: datetime | None
    created_at: datetime

    @staticmethod
    def resolve_student_name(obj):
        return str(obj.student) if obj.student else ""

    @staticmethod
    def resolve_booking_id(obj):
        booking = getattr(obj, "personal_drop_in_booking", None)
        if booking is None or booking.club_id != obj.club_id:
            return None
        return booking.id


class DebtWriteOffIn(Schema):
    reason: str = ""


# --- Freeze ---


class FreezeIn(Schema):
    days: int
    reason: str  # "vacation" | "injury" | "illness" | "other"


class FreezeRejectIn(Schema):
    decision_reason: str = ""


class FreezeOut(Schema):
    id: int
    subscription_id: int
    days: int
    reason: str
    status: str
    approved_by_id: int | None
    rejected_by_id: int | None
    decision_at: datetime | None
    decision_reason: str
    starts_at: datetime
    ends_at: datetime | None
    created_at: datetime


class DebtorFilters(Schema):
    group_id: int | None = None
    trainer_id: int | None = None
    location_id: int | None = None
    student_status: str | None = None
    date_from: date | None = None
    date_to: date | None = None


# --- ClubSettings ---


class ClubSettingsOut(Schema):
    freeze_enabled: bool
    freeze_max_days: int
    freeze_max_count: int | None
    timezone: str
    primary_color: str
    accent_color: str
    club_name_display: str
    logo_url: str
    feedback_delay_hours: int
    max_push_per_week: int
    quiet_hours_start: dt.time
    quiet_hours_end: dt.time


class ClubSettingsIn(Schema):
    freeze_enabled: bool | None = None
    freeze_max_days: int | None = None
    freeze_max_count: int | None = None
    primary_color: str | None = None
    accent_color: str | None = None
    club_name_display: str | None = None
    logo_url: str | None = None

    @staticmethod
    def _validate_hex_color(value: str | None) -> str | None:
        if value is None:
            return value
        import re

        if not re.match(r"^#[0-9A-Fa-f]{6}$", value):
            raise ValueError("Must be hex color #RRGGBB")
        return value

    def model_post_init(self, __context) -> None:
        # Validate hex colors on input
        if self.primary_color is not None:
            self._validate_hex_color(self.primary_color)
        if self.accent_color is not None:
            self._validate_hex_color(self.accent_color)


# --- Expenses ---


class ExpenseIn(Schema):
    name: str
    amount: Decimal
    date: date
    category: str = ""
    is_recurring: bool = False


class ExpenseOut(Schema):
    id: int
    name: str
    amount: Decimal
    date: date
    category: str
    is_recurring: bool
    created_at: datetime


class ExpenseUpdate(Schema):
    name: str | None = None
    amount: Decimal | None = None
    date: dt.date | None = None
    category: str | None = None
    is_recurring: bool | None = None
