from datetime import date, timedelta

from ninja import Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from apps.clubs.models import ClubMembership
from apps.clubs.timezones import club_localdate
from apps.common.permissions import role_required
from apps.trainers.models import Trainer
from apps.trainers.schemas import (
    EarningSummaryOut,
    SalarySummaryOut,
    TrainerIn,
    TrainerLocationIn,
    TrainerLocationOut,
    TrainerOut,
    TrainerSalaryLedgerRowOut,
    TrainerSettlementSummaryOut,
    TrainerUpdate,
)
from apps.trainers.selectors import (
    get_salary_summary,
    get_trainer_by_id,
    get_trainer_earnings_summary,
    get_trainer_for_user,
    get_trainer_salary_ledger_rows,
    get_trainers,
)
from apps.trainers.services import create_trainer, update_trainer, update_trainer_locations

router = Router(tags=["trainers"])

MAX_EARNINGS_RANGE_DAYS = 366


@router.get("/{trainer_id}/settlements/summary/", response=TrainerSettlementSummaryOut)
@role_required("owner", "admin", "trainer")
def trainer_settlement_summary(request, trainer_id: int):
    from apps.trainers.settlement_selectors import get_trainer_settlement_summary

    if request._membership.role == ClubMembership.Role.TRAINER:
        try:
            own = get_trainer_for_user(club=request.club, user=request.user)
        except Trainer.DoesNotExist:
            raise HttpError(403, "Trainer profile not found")
        if own.id != trainer_id:
            raise HttpError(403, "Trainers can only view their own settlements")
    if not Trainer.objects.for_club(request.club).filter(id=trainer_id).exists():
        raise HttpError(404, "Trainer not found")
    date_from, date_to = _parse_date_range(request)
    result = get_trainer_settlement_summary(
        club=request.club, trainer_id=trainer_id, date_from=date_from, date_to=date_to,
    )
    return {**result, "opening_on": result["opening"].effective_on if result["opening"] else None,
            "unresolved_count": len(result["unresolved_ids"])}


@router.get("/me/", response=TrainerOut)
@role_required("trainer")
def get_my_trainer_profile(request):
    """Resolve current user's Trainer record."""
    from apps.trainers.selectors import get_trainer_for_user

    try:
        return get_trainer_for_user(club=request.club, user=request.user)
    except Trainer.DoesNotExist:
        raise HttpError(404, "Trainer profile not found for current user")


@router.get("/", response=list[TrainerOut])
@role_required("owner", "admin")
@paginate(LimitOffsetPagination)
def list_trainers(request):
    return get_trainers(club=request.club)


@router.post("/", response={201: TrainerOut})
@role_required("owner")
def create_trainer_endpoint(request, payload: TrainerIn):
    locations_data = [
        {
            "location_id": loc.location_id,
            "rate_group": loc.rate_group,
            "rate_personal": loc.rate_personal,
            "rate_mini_group": loc.rate_mini_group,
            "rates": [
                {
                    "training_type_id": rate.training_type_id,
                    "percent": rate.percent,
                }
                for rate in loc.rates
            ],
        }
        for loc in payload.locations
    ] if payload.locations else None
    trainer = create_trainer(
        club_id=request.club.id,
        first_name=payload.first_name,
        last_name=payload.last_name,
        phone=payload.phone,
        locations=locations_data,
    )
    return 201, trainer


# ──────────────────────────────────────────────
# Salary / Earnings endpoints (before /{trainer_id}/ to avoid URL conflict)
# ──────────────────────────────────────────────


def _parse_date_range(request) -> tuple[date, date]:
    date_from_str = request.GET.get("date_from")
    date_to_str = request.GET.get("date_to")
    if not date_from_str or not date_to_str:
        # Default: current month
        today = club_localdate(request.club)
        date_from = today.replace(day=1)
        date_to = today
    else:
        try:
            date_from = date.fromisoformat(date_from_str)
            date_to = date.fromisoformat(date_to_str)
        except ValueError:
            raise HttpError(400, "Invalid date format. Use YYYY-MM-DD.")
        if date_from > date_to:
            raise HttpError(400, "date_from must be <= date_to")
    if date_to - date_from >= timedelta(days=MAX_EARNINGS_RANGE_DAYS):
        raise HttpError(400, f"date range must be shorter than {MAX_EARNINGS_RANGE_DAYS} days")
    return date_from, date_to


@router.get("/salary-summary/", response=list[SalarySummaryOut])
@role_required("owner", "admin")
def salary_summary(request):
    date_from, date_to = _parse_date_range(request)
    return get_salary_summary(club=request.club, date_from=date_from, date_to=date_to)


# ──────────────────────────────────────────────
# Trainer detail routes (/{trainer_id}/)
# ──────────────────────────────────────────────


@router.get("/{trainer_id}/", response=TrainerOut)
@role_required("owner", "admin")
def get_trainer_detail(request, trainer_id: int):
    return get_trainer_by_id(club=request.club, trainer_id=trainer_id)


@router.put("/{trainer_id}/", response=TrainerOut)
@role_required("owner")
def update_trainer_endpoint(request, trainer_id: int, payload: TrainerUpdate):
    fields = {}
    for field_name in ("first_name", "last_name", "phone", "is_active"):
        if field_name in payload.model_fields_set:
            fields[field_name] = getattr(payload, field_name)
    return update_trainer(trainer_id=trainer_id, club_id=request.club.id, **fields)


@router.put("/{trainer_id}/locations/", response=list[TrainerLocationOut])
@role_required("owner")
def update_locations_endpoint(request, trainer_id: int, payload: list[TrainerLocationIn]):
    return update_trainer_locations(
        trainer_id=trainer_id,
        club_id=request.club.id,
        locations=[
            {
                "location_id": loc.location_id,
                "rate_group": loc.rate_group,
                "rate_personal": loc.rate_personal,
                "rate_mini_group": loc.rate_mini_group,
                "rates": [
                    {
                        "training_type_id": rate.training_type_id,
                        "percent": rate.percent,
                    }
                    for rate in loc.rates
                ],
            }
            for loc in payload
        ],
    )


@router.get("/{trainer_id}/earnings/", response=list[TrainerSalaryLedgerRowOut])
@role_required("owner", "admin", "trainer")
def trainer_earnings(request, trainer_id: int):
    # Trainers can only view their own earnings
    if request._membership.role == ClubMembership.Role.TRAINER:
        try:
            trainer = get_trainer_for_user(club=request.club, user=request.user)
        except Trainer.DoesNotExist:
            raise HttpError(403, "Trainer profile not found")
        if trainer.id != trainer_id:
            raise HttpError(403, "Trainers can only view their own earnings")
    date_from, date_to = _parse_date_range(request)
    return get_trainer_salary_ledger_rows(
        club=request.club,
        trainer_id=trainer_id,
        date_from=date_from,
        date_to=date_to,
    )


@router.get("/{trainer_id}/earnings/summary/", response=EarningSummaryOut)
@role_required("owner", "admin", "trainer")
def trainer_earnings_summary(request, trainer_id: int):
    if request._membership.role == ClubMembership.Role.TRAINER:
        try:
            trainer = get_trainer_for_user(club=request.club, user=request.user)
        except Trainer.DoesNotExist:
            raise HttpError(403, "Trainer profile not found")
        if trainer.id != trainer_id:
            raise HttpError(403, "Trainers can only view their own earnings")
    date_from, date_to = _parse_date_range(request)
    return get_trainer_earnings_summary(club=request.club, trainer_id=trainer_id, date_from=date_from, date_to=date_to)
