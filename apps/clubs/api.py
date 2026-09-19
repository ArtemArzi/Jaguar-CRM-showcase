from django.http import HttpResponseForbidden
from ninja import Router

from apps.clubs.models import Location
from apps.clubs.schemas import (
    ClubOut,
    ClubUpdateIn,
    CommercialJourneyCapabilityOut,
    LocationOut,
    MemberOut,
    RegisterClubIn,
)
from apps.clubs.services import register_club, update_club_settings
from apps.common.permissions import role_required
from apps.common.schemas import schema_sent_fields

router = Router(tags=["clubs"])
_CLUB_UPDATE_FIELDS = ("name", "city", "disciplines")


@router.post("/register/", response={201: ClubOut}, auth=None)
def register_club_endpoint(request, payload: RegisterClubIn):
    """Register a new club. Requires authenticated user (via allauth session/JWT)."""
    if not request.user or not request.user.is_authenticated:
        return HttpResponseForbidden("Authentication required")
    club = register_club(
        user=request.user,
        name=payload.name,
        city=payload.city,
        disciplines=payload.disciplines,
        location_name=payload.location_name,
    )
    return 201, club


@router.get("/me/", response=ClubOut)
@role_required("owner", "admin", "trainer", "student", "parent")
def get_my_club(request):
    """Any authenticated member can see their club info."""
    return request.club


@router.get("/commercial-journey-capability/", response=CommercialJourneyCapabilityOut)
@role_required("owner", "admin")
def get_commercial_journey_capability_endpoint(request):
    """Expose the tenant-owned rollout readiness projection without mutating it."""

    from apps.attendance.training_group_selectors import get_training_group_payment_selection_capability
    from apps.billing.service_modules.payment_readiness import get_online_payment_capability
    from apps.clubs.capabilities import (
        get_commercial_journey_capability,
        get_v1_commercial_journey_command_availability,
        get_v2_group_manual_admission_command_availability,
        get_v2_group_provider_command_availability,
        get_v2_manual_admission_command_availability,
        get_v2_provider_command_availability,
    )

    capability = get_commercial_journey_capability(club=request.club)
    group_capability = get_training_group_payment_selection_capability(club=request.club)
    provider_capability = get_online_payment_capability()
    return CommercialJourneyCapabilityOut(
        commercial_journey_protocol_version=capability.protocol_version,
        unified_client_journey_enabled=capability.unified_client_journey_enabled,
        manual_operational_admission_enabled=capability.manual_operational_admission_enabled,
        v2_commercial_journey_enabled=capability.v2_commercial_journey_enabled,
        v2_manual_admission_enabled=capability.v2_manual_admission_enabled,
        v1_new_command_status=get_v1_commercial_journey_command_availability(
            capability=capability,
        ).code,
        v2_personal_manual_command_status=get_v2_manual_admission_command_availability(
            capability=capability,
        ).code,
        v2_personal_sbp_command_status=get_v2_provider_command_availability(
            capability=capability,
            provider_creation_enabled=provider_capability.enabled,
        ).code,
        v2_group_manual_command_status=get_v2_group_manual_admission_command_availability(
            capability=capability,
            training_group_rollout_mode=str(group_capability["training_group_rollout_mode"]),
            training_group_new_writes_enabled=(
                group_capability["training_group_payment_selection_mode"] == "canonical"
            ),
        ).code,
        v2_group_sbp_command_status=get_v2_group_provider_command_availability(
            capability=capability,
            provider_creation_enabled=provider_capability.enabled,
            training_group_rollout_mode=str(group_capability["training_group_rollout_mode"]),
            training_group_new_writes_enabled=(
                group_capability["training_group_payment_selection_mode"] == "canonical"
            ),
        ).code,
        accepted_replay_status=get_v2_manual_admission_command_availability(
            capability=capability,
            accepted_replay_or_drain=True,
        ).code,
    )


@router.put("/settings/", response=ClubOut)
@role_required("owner")
def update_club(request, payload: ClubUpdateIn):
    """Only owner can update club info (name, city, disciplines)."""
    fields = schema_sent_fields(payload, _CLUB_UPDATE_FIELDS)
    return update_club_settings(
        club_id=request.club.id,
        name=fields.get("name"),
        city=fields.get("city"),
        disciplines=fields.get("disciplines"),
        update_name="name" in fields,
        update_city="city" in fields,
        update_disciplines="disciplines" in fields,
    )


@router.get("/locations/", response=list[LocationOut])
@role_required("owner", "admin", "trainer")
def list_locations(request):
    """List club locations (for schedule form dropdown)."""
    return list(Location.objects.filter(club=request.club).order_by("name"))


@router.get("/members/", response=list[MemberOut])
@role_required("owner", "admin")
def list_members(request):
    """Owner and admin can see all club members."""
    from apps.clubs.models import ClubMembership

    memberships = ClubMembership.objects.filter(club=request.club, is_active=True).select_related("user")
    return [{"id": m.id, "user_email": m.user.email, "role": m.role} for m in memberships]
