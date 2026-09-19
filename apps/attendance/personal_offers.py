"""Attendance read/acceptance helpers for billing-owned personal offers."""

from __future__ import annotations

import hashlib
import json

from apps.attendance.models import PersonalAvailabilitySlot
from apps.billing.service_modules.personal_offers import PersonalBookingOffer


def _offer_fields(*, offer: PersonalBookingOffer) -> dict:
    tariff = offer.tariff
    discount = offer.discount
    return {
        "offer_tariff_id": tariff.id,
        "offer_tariff_name": tariff.name,
        # Keep the established public field as the ordinary price.  The payable
        # amount is explicit so no consumer needs to infer discount arithmetic.
        "offer_price": str(offer.base_amount),
        "offer_trainer_id": tariff.personal_booking_trainer_id,
        "offer_base_amount": str(offer.base_amount),
        "offer_discount_id": discount.id if discount is not None else None,
        "offer_discount_name": discount.name if discount is not None else "",
        "offer_discount_type": discount.discount_type if discount is not None else "",
        "offer_discount_value": str(discount.value) if discount is not None else "",
        "offer_discount_amount": str(offer.discount_amount),
        "offer_payable_amount": str(offer.payable_amount),
        "offer_duration_days": tariff.duration_days,
        "offer_scope": tariff.scope,
        "offer_location_id": tariff.location_id,
    }


def _offer_canonical(*, offer: PersonalBookingOffer) -> dict:
    tariff = offer.tariff
    component = offer.component
    discount = offer.discount
    return {
        "tariff": {
            "id": tariff.id,
            "updated_at": tariff.updated_at.isoformat(),
            "price": str(tariff.price),
            "duration_days": tariff.duration_days,
            "scope": tariff.scope,
            "location_id": tariff.location_id,
            "personal_booking_trainer_id": tariff.personal_booking_trainer_id,
            "trainer_payout_policy": tariff.trainer_payout_policy or "on_checkin",
        },
        "component": {
            "id": component.id,
            "updated_at": component.updated_at.isoformat(),
            "name": component.name,
            "training_type_id": component.training_type_id,
            "entitlement_kind": component.entitlement_kind,
            "credits_total": component.credits_total,
            "scope": component.scope,
            "location_id": component.location_id,
            "trainer_payout_policy": component.trainer_payout_policy,
            "paid_amount_basis": str(component.paid_amount_basis),
        },
        "amounts": {
            "base_amount": str(offer.base_amount),
            "discount_amount": str(offer.discount_amount),
            "payable_amount": str(offer.payable_amount),
        },
        "discount": {
            "id": discount.id if discount is not None else None,
            "updated_at": discount.updated_at.isoformat() if discount is not None else None,
            "name": discount.name if discount is not None else "",
            "type": discount.discount_type if discount is not None else "",
            "value": str(discount.value) if discount is not None else "",
        },
    }


def personal_offer_payload(
    *,
    slot: PersonalAvailabilitySlot,
    offer: PersonalBookingOffer,
) -> dict:
    """Return a safe, slot-bound representation of the displayed offer."""
    canonical = {
        "slot": {
            "id": slot.id,
            "starts_at": slot.starts_at.isoformat(),
            "ends_at": slot.ends_at.isoformat(),
            "updated_at": slot.updated_at.isoformat(),
        },
        **_offer_canonical(offer=offer),
    }
    digest = hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        **_offer_fields(offer=offer),
        "offer_digest": digest,
    }


def direct_personal_offer_payload(
    *,
    offer: PersonalBookingOffer,
    trainer_id: int | None = None,
    starts_at=None,
    ends_at=None,
    location_id: int | None = None,
    training_type_id: int | None = None,
) -> dict:
    """Return a direct-time offer and, when exact context is supplied, its digest."""
    payload = _offer_fields(offer=offer)
    if None not in {trainer_id, starts_at, ends_at, location_id, training_type_id}:
        canonical = {
            "direct": {
                "trainer_id": trainer_id,
                "starts_at": starts_at.isoformat(),
                "ends_at": ends_at.isoformat(),
                "location_id": location_id,
                "training_type_id": training_type_id,
            },
            **_offer_canonical(offer=offer),
        }
        payload.update(
            {
                "offer_digest": hashlib.sha256(
                    json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "offer_error_code": "",
            }
        )
    return payload
