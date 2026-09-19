from ninja import Query, Router
from ninja.errors import HttpError

from apps.attendance.services.staff_intents import get_personal_commercial_context
from apps.clubs.capabilities import is_unified_client_journey_enabled
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import role_required
from apps.leads.schemas import (
    AssignLeadIn,
    BookTrialIn,
    ContactOutcomeIn,
    ContactOutcomeOut,
    CreateLeadIn,
    LeadActionContextOut,
    LeadFunnelOut,
    LeadOut,
    LeadPoolOut,
    LoseLeadIn,
    ReleaseLeadIn,
    UpdateLeadStatusIn,
)
from apps.leads.selectors import (
    ACTIVE_LEAD_WORKSPACE,
    VALID_LEAD_WORKSPACES,
    get_lead_action_context,
    get_lead_action_contexts,
    get_lead_funnel_stats,
    get_leads,
)
from apps.leads.services import (
    LeadClaimConflictError,
    assign_lead,
    book_trial,
    claim_lead,
    convert_lead,
    create_lead,
    lose_lead,
    record_contact_outcome,
    release_lead,
    reopen_lead,
    update_lead_status,
)
from apps.students.duplicates import DuplicateStudentError
from apps.students.models import Student
from apps.students.schemas import StudentCommercialContextOut
from apps.students.scopes import trainer_student_scope_filter

router = Router(tags=["leads"])

VALID_LEAD_SCOPES = {"mine", "pool", "all"}


def _get_current_trainer(request):
    from apps.trainers.models import Trainer
    from apps.trainers.selectors import get_trainer_for_user

    try:
        return get_trainer_for_user(club=request.club, user=request.user)
    except Trainer.DoesNotExist:
        raise HttpError(403, "Trainer profile not found")


def _assert_trainer_can_access_lead(request, lead_id: int) -> None:
    if request._membership.role != "trainer":
        return

    trainer = _get_current_trainer(request)
    if not Student.objects.for_club(request.club).filter(
        id=lead_id,
        lead_status__isnull=False,
        deleted_at__isnull=True,
        assigned_trainer_id=trainer.id,
    ).exists():
        raise HttpError(403, "Access denied: not your lead")


def _required_assigned_trainer_id(request) -> int | None:
    if request._membership.role != "trainer":
        return None
    return _get_current_trainer(request).id


def _raise_scoped_mutation_error(exc: BusinessLogicError) -> None:
    if exc.code in {"not_a_lead", "not_your_lead"}:
        raise HttpError(404, "Lead not found")
    raise exc


def _lead_payload(
    lead: Student,
    *,
    workspace: str = "leads_active",
    action_context: dict | None = None,
) -> dict:
    payload = {
        "id": lead.id,
        "first_name": lead.first_name,
        "last_name": lead.last_name,
        "phone": lead.phone,
        "guardian_phone": lead.guardian_phone,
        "is_child": lead.is_child,
        "status": lead.status,
        "lead_status": lead.lead_status,
        "loss_reason": lead.loss_reason,
        "assigned_trainer_id": lead.assigned_trainer_id,
        "trial_date": lead.trial_date,
        "source": lead.source,
        "created_at": lead.created_at,
        "workspace": workspace,
    }
    if action_context is not None:
        payload["primary_action"] = action_context["primary_action"]
    return payload


def _contact_phone(lead: Student) -> str:
    return lead.phone or lead.guardian_phone


def _pool_lead_payload(lead: Student) -> dict:
    return LeadPoolOut(
        id=lead.id,
        first_name=lead.first_name,
        last_name=lead.last_name,
        masked_phone=_mask_phone(_contact_phone(lead)),
        is_child=lead.is_child,
        status=lead.status,
        lead_status=lead.lead_status,
        loss_reason=lead.loss_reason,
        assigned_trainer_id=lead.assigned_trainer_id,
        trial_date=lead.trial_date,
        source=lead.source,
        created_at=lead.created_at,
    ).dict()


def _mask_phone(phone: str) -> str:
    if len(phone) <= 4:
        return "****"
    visible_prefix = phone[:4]
    visible_suffix = phone[-2:]
    return f"{visible_prefix}****{visible_suffix}"


def _duplicate_response(request, exc: DuplicateStudentError):
    existing = exc.existing_student
    role = request._membership.role
    can_open = role != "trainer"
    duplicate_scope = "club"

    if role == "trainer":
        trainer = _get_current_trainer(request)
        can_open = (
            Student.objects.for_club(request.club)
            .filter(id=existing.id, deleted_at__isnull=True)
            .filter(trainer_student_scope_filter(trainer.id))
            .exists()
        )
        if can_open:
            duplicate_scope = "own"
        elif existing.lead_status is not None and existing.assigned_trainer_id is None:
            duplicate_scope = "pool"
        else:
            duplicate_scope = "other"

    payload = {
        "detail": "Этот телефон уже есть в CRM",
        "code": exc.code,
        "duplicate_scope": duplicate_scope,
        "can_open_existing": can_open,
    }
    if can_open:
        payload["existing_student"] = {
            "id": existing.id,
            "display_name": " ".join(
                part for part in [existing.first_name, existing.last_name] if part
            ),
            "is_child": existing.is_child,
            "status": existing.status,
            "lead_status": existing.lead_status,
            "assigned_trainer_id": existing.assigned_trainer_id,
        }
    return 409, payload


@router.post("/", response={201: LeadOut, 409: dict})
@role_required("owner", "admin", "trainer")
def create_lead_endpoint(request, payload: CreateLeadIn):
    assigned_trainer_id = payload.assigned_trainer_id
    if request._membership.role == "trainer":
        assigned_trainer_id = _get_current_trainer(request).id
    try:
        lead = create_lead(
            club_id=request.club.id,
            first_name=payload.first_name,
            last_name=payload.last_name,
            phone=payload.phone,
            guardian_phone=payload.guardian_phone,
            date_of_birth=payload.date_of_birth,
            is_child=payload.is_child,
            source=payload.source,
            assigned_trainer_id=assigned_trainer_id,
        )
    except DuplicateStudentError as exc:
        return _duplicate_response(request, exc)
    return 201, lead


@router.get("/")
@role_required("owner", "admin", "trainer")
def list_leads_endpoint(
    request,
    status: str | None = Query(None),
    assigned_trainer_id: int | None = Query(None),
    scope: str = Query("all"),
    limit: int = Query(100),
    offset: int = Query(0),
    workspace: str = Query(ACTIVE_LEAD_WORKSPACE),
):
    if scope not in VALID_LEAD_SCOPES:
        raise HttpError(400, "Invalid lead scope")
    if limit < 1 or limit > 200:
        raise HttpError(400, "limit must be between 1 and 200")
    if offset < 0:
        raise HttpError(400, "offset must be greater than or equal to 0")
    unified_enabled = is_unified_client_journey_enabled(club=request.club)
    if not unified_enabled:
        workspace = ACTIVE_LEAD_WORKSPACE
    elif workspace not in VALID_LEAD_WORKSPACES:
        raise HttpError(400, "Invalid lead workspace")

    current_trainer_id = None
    if request._membership.role == "trainer":
        current_trainer_id = _get_current_trainer(request).id
        if scope != "pool":
            scope = "mine"
        assigned_trainer_id = None

    qs = get_leads(
        club=request.club,
        status=status,
        assigned_trainer_id=assigned_trainer_id,
        scope=scope,
        current_trainer_id=current_trainer_id,
        workspace=workspace,
    )
    count = qs.count()
    page = list(qs[offset : offset + limit])
    if scope == "pool":
        items = [_pool_lead_payload(lead) for lead in page]
    else:
        action_contexts = (
            get_lead_action_contexts(club=request.club, leads=page)
            if unified_enabled and workspace == ACTIVE_LEAD_WORKSPACE
            else {}
        )
        items = [
            _lead_payload(
                lead,
                workspace=("leads_archived" if workspace == "archived" else "leads_active"),
                action_context=action_contexts.get(lead.id),
            )
            for lead in page
        ]
    return {"count": count, "items": items}


@router.get("/funnel", response=LeadFunnelOut)
@role_required("owner", "admin")
def funnel_stats_endpoint(request):
    return get_lead_funnel_stats(club=request.club)


@router.get("/{lead_id}", response=LeadOut)
@role_required("owner", "admin", "trainer")
def get_lead_endpoint(request, lead_id: int):
    if not is_unified_client_journey_enabled(club=request.club):
        _assert_trainer_can_access_lead(request, lead_id)
        return Student.objects.for_club(request.club).get(
            id=lead_id, lead_status__isnull=False, deleted_at__isnull=True
        )

    lead = _get_unified_authorized_lead(request=request, lead_id=lead_id)
    workspace = "leads_active" if lead.lead_status is not None else "leads_archived"
    action_context = (
        get_lead_action_context(club=request.club, lead=lead)
        if workspace == "leads_active"
        else None
    )
    return _lead_payload(lead, workspace=workspace, action_context=action_context)


def _get_unified_authorized_lead(request, lead_id: int) -> Student:
    lead = Student.objects.for_club(request.club).filter(
        id=lead_id,
        deleted_at__isnull=True,
    ).first()
    is_active = lead is not None and lead.lead_status is not None
    is_archived = (
        lead is not None
        and lead.lead_status is None
        and lead.became_student_at is None
        and lead.status == Student.Status.LOST
    )
    if lead is None or not (is_active or is_archived):
        raise HttpError(404, "Lead not found")
    if request._membership.role == "trainer":
        trainer = _get_current_trainer(request)
        if lead.assigned_trainer_id != trainer.id:
            raise HttpError(404, "Lead not found")
    return lead


@router.get("/{lead_id}/action-context", response=LeadActionContextOut)
@role_required("owner", "admin", "trainer")
def get_lead_action_context_endpoint(request, lead_id: int):
    if not is_unified_client_journey_enabled(club=request.club):
        raise HttpError(404, "Lead action context is unavailable")
    lead = _get_unified_authorized_lead(request=request, lead_id=lead_id)
    if lead.lead_status is None:
        raise HttpError(409, "Archived lead has no active action context")
    return get_lead_action_context(club=request.club, lead=lead)


@router.get("/{lead_id}/commercial-context/", response=StudentCommercialContextOut)
@role_required("owner", "admin", "trainer")
def get_lead_commercial_context_endpoint(request, lead_id: int):
    """Return persistent personal-commercial receipts for an active lead.

    The lead workspace remains the authority for whether this route is useful:
    a converted/lost student cannot be opened through it.  Payment, booking and
    retry state itself is sourced from the immutable personal artifacts.
    """
    _assert_trainer_can_access_lead(request, lead_id)
    lead = Student.objects.for_club(request.club).filter(
        id=lead_id,
        lead_status__isnull=False,
        deleted_at__isnull=True,
    ).first()
    if lead is None:
        raise HttpError(404, "Lead not found")
    return {
        "student_id": lead.id,
        "attempts": get_personal_commercial_context(
            club_id=request.club.id,
            student_id=lead.id,
            trainer_id=_get_current_trainer(request).id if request._membership.role == "trainer" else None,
            actor_role=request._membership.role,
        ),
    }


@router.post("/{lead_id}/contact-outcomes/", response=ContactOutcomeOut)
@role_required("owner", "admin", "trainer")
def record_contact_outcome_endpoint(request, lead_id: int, payload: ContactOutcomeIn):
    if not is_unified_client_journey_enabled(club=request.club):
        raise HttpError(404, "Contact outcomes are unavailable")
    _get_unified_authorized_lead(request=request, lead_id=lead_id)
    required_trainer_id = _required_assigned_trainer_id(request)
    try:
        lead, next_flow = record_contact_outcome(
            club_id=request.club.id,
            student_id=lead_id,
            outcome=payload.outcome,
            due_date=payload.due_date,
            loss_reason=payload.loss_reason,
            notes=payload.notes,
            actor_user_id=request.user.id,
            required_assigned_trainer_id=required_trainer_id,
        )
    except BusinessLogicError as exc:
        _raise_scoped_mutation_error(exc)
    action_context = (
        get_lead_action_context(club=request.club, lead=lead)
        if lead.lead_status is not None
        else None
    )
    return {
        "lead": _lead_payload(lead, action_context=action_context),
        "action_context": action_context,
        "next_flow": next_flow,
    }


@router.post("/{lead_id}/reopen", response=LeadOut)
@role_required("owner", "admin", "trainer")
def reopen_lead_endpoint(request, lead_id: int):
    if not is_unified_client_journey_enabled(club=request.club):
        raise HttpError(404, "Lead reopen is unavailable")
    lead = _get_unified_authorized_lead(request=request, lead_id=lead_id)
    if lead.lead_status is not None:
        raise HttpError(409, "Lead is not archived")
    required_trainer_id = _required_assigned_trainer_id(request)
    try:
        reopened = reopen_lead(
            club_id=request.club.id,
            student_id=lead_id,
            actor_user_id=request.user.id,
            required_assigned_trainer_id=required_trainer_id,
        )
    except BusinessLogicError as exc:
        if exc.code == "lead_not_archived":
            raise HttpError(409, "Lead is no longer archived")
        _raise_scoped_mutation_error(exc)
    action_context = get_lead_action_context(club=request.club, lead=reopened)
    return _lead_payload(reopened, action_context=action_context)


@router.post("/{lead_id}/reopen-and-claim", response=LeadOut)
@role_required("trainer")
def reopen_and_claim_lead_endpoint(request, lead_id: int):
    if not is_unified_client_journey_enabled(club=request.club):
        raise HttpError(404, "Lead reopen is unavailable")
    trainer = _get_current_trainer(request)
    archived_exists = Student.objects.for_club(request.club).filter(
        id=lead_id,
        deleted_at__isnull=True,
        lead_status__isnull=True,
        became_student_at__isnull=True,
        status=Student.Status.LOST,
        assigned_trainer__isnull=True,
    ).exists()
    if not archived_exists:
        raise HttpError(404, "Lead not found")
    try:
        reopened = reopen_lead(
            club_id=request.club.id,
            student_id=lead_id,
            actor_user_id=request.user.id,
            claim_trainer_id=trainer.id,
        )
    except LeadClaimConflictError as exc:
        raise HttpError(409, exc.message)
    except BusinessLogicError as exc:
        if exc.code == "lead_not_archived":
            raise HttpError(409, "Lead was already claimed")
        _raise_scoped_mutation_error(exc)
    action_context = get_lead_action_context(club=request.club, lead=reopened)
    return _lead_payload(reopened, action_context=action_context)


@router.post("/{lead_id}/status", response=LeadOut)
@role_required("owner", "admin", "trainer")
def update_status_endpoint(request, lead_id: int, payload: UpdateLeadStatusIn):
    _assert_trainer_can_access_lead(request, lead_id)
    try:
        return update_lead_status(
            club_id=request.club.id,
            student_id=lead_id,
            new_status=payload.status,
            actor_user_id=request.user.id,
            required_assigned_trainer_id=_required_assigned_trainer_id(request),
        )
    except BusinessLogicError as exc:
        _raise_scoped_mutation_error(exc)


@router.post("/{lead_id}/book-trial", response=LeadOut)
@role_required("owner", "admin", "trainer")
def book_trial_endpoint(request, lead_id: int, payload: BookTrialIn):
    _assert_trainer_can_access_lead(request, lead_id)
    trainer_id = payload.trainer_id
    required_trainer_id = None
    required_lead_trainer_id = None
    if request._membership.role == "trainer":
        current_trainer = _get_current_trainer(request)
        required_trainer_id = current_trainer.id
        required_lead_trainer_id = current_trainer.id
        if payload.mode == "personal":
            trainer_id = current_trainer.id
    try:
        return book_trial(
            club_id=request.club.id,
            student_id=lead_id,
            trial_date=payload.trial_date,
            schedule_id=payload.schedule_id,
            occurrence_date=payload.occurrence_date,
            required_trainer_id=required_trainer_id,
            mode=payload.mode,
            starts_at=payload.starts_at,
            ends_at=payload.ends_at,
            trainer_id=trainer_id,
            location_id=payload.location_id,
            training_type_id=payload.training_type_id,
            actor_user_id=request.user.id,
            required_assigned_trainer_id=required_lead_trainer_id,
        )
    except BusinessLogicError as exc:
        _raise_scoped_mutation_error(exc)


@router.post("/{lead_id}/convert", response=LeadOut)
@role_required("owner", "admin")
def convert_lead_endpoint(request, lead_id: int):
    return convert_lead(club_id=request.club.id, student_id=lead_id, actor_user_id=request.user.id)


@router.post("/{lead_id}/lose", response=LeadOut)
@role_required("owner", "admin", "trainer")
def lose_lead_endpoint(request, lead_id: int, payload: LoseLeadIn):
    _assert_trainer_can_access_lead(request, lead_id)
    try:
        return lose_lead(
            club_id=request.club.id,
            student_id=lead_id,
            loss_reason=payload.loss_reason,
            actor_user_id=request.user.id,
            required_assigned_trainer_id=_required_assigned_trainer_id(request),
        )
    except BusinessLogicError as exc:
        _raise_scoped_mutation_error(exc)


@router.post("/{lead_id}/claim", response=LeadOut)
@role_required("trainer")
def claim_lead_endpoint(request, lead_id: int):
    trainer = _get_current_trainer(request)
    try:
        return claim_lead(
            club_id=request.club.id,
            student_id=lead_id,
            trainer_id=trainer.id,
            actor_user_id=request.user.id,
        )
    except LeadClaimConflictError as exc:
        raise HttpError(409, exc.message)
    except BusinessLogicError as exc:
        _raise_scoped_mutation_error(exc)


@router.post("/{lead_id}/release", response=LeadOut)
@role_required("trainer")
def release_lead_endpoint(request, lead_id: int, payload: ReleaseLeadIn):
    _assert_trainer_can_access_lead(request, lead_id)
    trainer = _get_current_trainer(request)
    return release_lead(
        club_id=request.club.id,
        student_id=lead_id,
        trainer_id=trainer.id,
        actor_user_id=request.user.id,
        reason=payload.reason,
    )


@router.post("/{lead_id}/assign", response=LeadOut)
@role_required("owner", "admin")
def assign_lead_endpoint(request, lead_id: int, payload: AssignLeadIn):
    return assign_lead(
        club_id=request.club.id,
        student_id=lead_id,
        trainer_id=payload.trainer_id,
        actor_user_id=request.user.id,
        reason=payload.reason,
    )
