from ninja import Router

from apps.common.permissions import role_required
from apps.onboarding.schemas import (
    FinishOnboardingIn,
    FinishOnboardingOut,
    OnboardingDraftOut,
    SaveStepIn,
)
from apps.onboarding.services import finish_onboarding, save_step, start_onboarding

router = Router(tags=["onboarding"])


@router.post("/start", response={200: OnboardingDraftOut})
@role_required("owner", "admin")
def start(request):
    draft = start_onboarding(club_id=request.club.id)
    return draft


@router.post("/step", response={200: OnboardingDraftOut})
@role_required("owner", "admin")
def save(request, body: SaveStepIn):
    draft = save_step(
        club_id=request.club.id,
        draft_id=body.draft_id,
        step=body.step,
        data=body.data,
    )
    return draft


@router.get("/draft", response={200: OnboardingDraftOut, 404: dict})
@role_required("owner", "admin")
def get_draft(request):
    from apps.onboarding.models import OnboardingDraft

    draft = OnboardingDraft.objects.for_club(request.club).filter(is_completed=False).first()
    if not draft:
        return 404, {"detail": "No active draft"}
    return start_onboarding(club_id=request.club.id)


@router.post("/finish", response={200: FinishOnboardingOut})
@role_required("owner", "admin")
def finish(request, body: FinishOnboardingIn):
    draft = finish_onboarding(club_id=request.club.id, draft_id=body.draft_id)
    return {"status": "completed", "draft": draft}
