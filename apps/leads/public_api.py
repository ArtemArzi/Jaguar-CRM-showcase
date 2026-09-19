from __future__ import annotations

from django.conf import settings
from ninja import Router
from ninja.errors import HttpError
from ninja.throttling import AnonRateThrottle

from apps.common.exceptions import BusinessLogicError
from apps.common.logging import hash_for_log
from apps.leads.schemas import LandingLeadIntakeIn, LeadIntakeAcceptedOut
from apps.leads.services import create_landing_lead_intake

router = Router(tags=["public-lead-intakes"])


@router.post(
    "/",
    auth=None,
    throttle=[AnonRateThrottle("5/m")],
    response={201: LeadIntakeAcceptedOut},
)
def create_public_lead_intake(request, payload: LandingLeadIntakeIn):
    if payload.hp_field.strip():
        raise BusinessLogicError("Invalid request", code="spam_detected")

    club_id = getattr(settings, "LANDING_DEFAULT_CLUB_ID", 0) or 0
    if club_id <= 0:
        raise HttpError(503, "Landing intake is not configured")

    event = create_landing_lead_intake(
        club_id=club_id,
        name=payload.name,
        phone=payload.phone,
        goal=payload.goal,
        preferred_format=payload.preferred_format,
        is_child=payload.is_child,
        consent=payload.consent.dict(),
        source=payload.source.dict(),
        request_id=_request_id(request),
        client_ip_hash=_client_ip_hash(request),
        user_agent=request.META.get("HTTP_USER_AGENT", ""),
        idempotency_key=payload.idempotency_key,
    )
    return 201, {"data": {"id": event.id, "status": "accepted"}}


def _request_id(request) -> str:
    request_id = getattr(request, "request_id", "") or request.META.get("HTTP_X_REQUEST_ID", "")
    return str(request_id).strip()[:128]


def _client_ip_hash(request) -> str:
    raw_ip = request.META.get("HTTP_X_FORWARDED_FOR", "").split(",", 1)[0].strip()
    if not raw_ip:
        raw_ip = request.META.get("REMOTE_ADDR", "")
    if not raw_ip:
        return ""
    return hash_for_log(raw_ip, salt=settings.SECRET_KEY)
