"""Immutable personal-service terms evidence written with a new intent."""

from __future__ import annotations

from decimal import Decimal

from apps.attendance.models import (
    PersonalBookingPaymentReservation,
    PersonalDropInBooking,
    PersonalServiceTermsSnapshot,
)
from apps.billing.models import Tariff
from apps.billing.service_modules.personal_offers import PersonalBookingOffer


def create_complete_personal_terms_snapshot(
    *,
    offer: PersonalBookingOffer,
    booking: PersonalDropInBooking | None = None,
    reservation: PersonalBookingPaymentReservation | None = None,
) -> PersonalServiceTermsSnapshot:
    """Persist the one-session contract; exactly one accepted target is required."""
    if (booking is None) == (reservation is None):
        raise ValueError("Exactly one personal terms target is required")
    target = booking or reservation
    tariff = offer.tariff
    component = offer.component
    location = tariff.location
    component_location = component.location
    snapshot = PersonalServiceTermsSnapshot(
        club_id=target.club_id,
        booking=booking,
        reservation=reservation,
        terms_version=PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V2,
        tariff_id_snapshot=tariff.id,
        tariff_name_snapshot=tariff.name,
        training_type_id_snapshot=tariff.training_type_id,
        training_type_name_snapshot=tariff.training_type.name,
        base_amount=offer.base_amount,
        discount_amount=offer.discount_amount,
        discount_id_snapshot=offer.discount.id if offer.discount is not None else None,
        discount_name_snapshot=offer.discount.name if offer.discount is not None else "",
        discount_type_snapshot=offer.discount.discount_type if offer.discount is not None else "",
        discount_value_snapshot=offer.discount.value if offer.discount is not None else None,
        payable_amount=offer.payable_amount,
        currency="RUB",
        duration_days=tariff.duration_days,
        scope=tariff.scope,
        location_id_snapshot=tariff.location_id,
        location_name_snapshot=location.name if location is not None else "",
        component_name_snapshot=component.name,
        component_training_type_id_snapshot=component.training_type_id,
        component_training_type_name_snapshot=component.training_type.name,
        component_entitlement_kind=component.entitlement_kind,
        component_credits_total=component.credits_total,
        component_scope=component.scope,
        component_location_id_snapshot=component.location_id,
        component_location_name_snapshot=(
            component_location.name if component_location is not None else ""
        ),
        tariff_trainer_payout_policy=tariff.trainer_payout_policy or "on_checkin",
        component_trainer_payout_policy=component.trainer_payout_policy,
        component_paid_amount_basis=offer.payable_amount,
        component_unit_amount_basis=(offer.payable_amount / component.credits_total),
    )
    snapshot.full_clean()
    snapshot.save()
    return snapshot


def clone_complete_personal_terms_snapshot(
    *,
    source_terms: PersonalServiceTermsSnapshot,
    booking: PersonalDropInBooking,
) -> PersonalServiceTermsSnapshot:
    """Copy accepted v2 evidence to a replacement booking without catalog reads.

    A payment-method correction is a new booking/payment attempt, so it needs
    its own append-only snapshot.  The source offer is already the authority;
    looking up today's tariff or discount here could silently reprice it.
    """

    if source_terms.terms_version != PersonalServiceTermsSnapshot.TermsVersion.COMPLETE_V2:
        raise ValueError("Only complete v2 personal terms can be cloned")
    snapshot = PersonalServiceTermsSnapshot(
        club_id=booking.club_id,
        booking=booking,
        terms_version=source_terms.terms_version,
        tariff_id_snapshot=source_terms.tariff_id_snapshot,
        tariff_name_snapshot=source_terms.tariff_name_snapshot,
        training_type_id_snapshot=source_terms.training_type_id_snapshot,
        training_type_name_snapshot=source_terms.training_type_name_snapshot,
        base_amount=source_terms.base_amount,
        discount_amount=source_terms.discount_amount,
        discount_id_snapshot=source_terms.discount_id_snapshot,
        discount_name_snapshot=source_terms.discount_name_snapshot,
        discount_type_snapshot=source_terms.discount_type_snapshot,
        discount_value_snapshot=source_terms.discount_value_snapshot,
        payable_amount=source_terms.payable_amount,
        currency=source_terms.currency,
        duration_days=source_terms.duration_days,
        scope=source_terms.scope,
        location_id_snapshot=source_terms.location_id_snapshot,
        location_name_snapshot=source_terms.location_name_snapshot,
        component_name_snapshot=source_terms.component_name_snapshot,
        component_training_type_id_snapshot=source_terms.component_training_type_id_snapshot,
        component_training_type_name_snapshot=source_terms.component_training_type_name_snapshot,
        component_entitlement_kind=source_terms.component_entitlement_kind,
        component_credits_total=source_terms.component_credits_total,
        component_scope=source_terms.component_scope,
        component_location_id_snapshot=source_terms.component_location_id_snapshot,
        component_location_name_snapshot=source_terms.component_location_name_snapshot,
        tariff_trainer_payout_policy=source_terms.tariff_trainer_payout_policy,
        component_trainer_payout_policy=source_terms.component_trainer_payout_policy,
        component_paid_amount_basis=source_terms.component_paid_amount_basis,
        component_unit_amount_basis=source_terms.component_unit_amount_basis,
    )
    snapshot.full_clean()
    snapshot.save()
    return snapshot


def create_legacy_partial_personal_terms_snapshot(
    *,
    tariff: Tariff,
    booking: PersonalDropInBooking | None = None,
    reservation: PersonalBookingPaymentReservation | None = None,
) -> PersonalServiceTermsSnapshot:
    """Dual-write safe evidence for a flag-off compatibility intent."""
    if (booking is None) == (reservation is None):
        raise ValueError("Exactly one personal terms target is required")
    target = booking or reservation
    amount = booking.price_snapshot if booking is not None else tariff.price
    snapshot = PersonalServiceTermsSnapshot(
        club_id=target.club_id,
        booking=booking,
        reservation=reservation,
        terms_version=PersonalServiceTermsSnapshot.TermsVersion.LEGACY_PARTIAL,
        tariff_id_snapshot=tariff.id,
        tariff_name_snapshot=(
            booking.tariff_name_snapshot if booking is not None else tariff.name
        ),
        base_amount=amount,
        discount_amount=Decimal("0.00"),
        payable_amount=amount,
    )
    snapshot.full_clean()
    snapshot.save()
    return snapshot
