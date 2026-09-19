from django.http import JsonResponse
from ninja import Router
from ninja.errors import HttpError

from apps.billing.models import Subscription
from apps.billing.schemas import BankPaymentOrderOut, SelfServiceBankPaymentOrderCreateIn
from apps.billing.selectors import get_bank_payment_orders
from apps.billing.services import cancel_bank_payment_order, create_bank_payment_order
from apps.clubs.capabilities import is_unified_client_journey_enabled
from apps.clubs.models import ClubMembership
from apps.common import auth_tokens
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import role_required
from apps.feedback.schemas import FormOut, SelfServiceResponseOut, SubmitSelfResponseIn
from apps.feedback.selectors import get_active_form
from apps.feedback.services import submit_feedback_response
from apps.students.parent_schemas import (
    AcceptInviteIn,
    AcceptInviteOut,
    ChildAttendanceOut,
    ChildProfileOut,
    ChildSummaryOut,
    CreateInviteIn,
    ParentInviteOut,
)
from apps.students.parent_selectors import (
    get_child_attendance,
    get_child_profile,
    get_parent_child,
    get_parent_children,
)
from apps.students.parent_services import accept_parent_invite, create_parent_invite

router = Router(tags=["parents"])


def _mark_bank_payment_order_cancel_scope(orders, *, allowed_sources: set[str]):
    order_list = list(orders)
    for order in order_list:
        order.can_cancel_source_allowed = order.source in allowed_sources
        # Refresh only coalesces this actor-scoped exact order onto durable
        # reconciliation; unlike cancellation it is safe across reused sources.
        order.can_refresh_source_allowed = True
        order.payment_action_mode = "self_service"
    return order_list


@router.get("/children/", response=list[ChildSummaryOut])
@role_required("parent")
def list_my_children(request):
    return get_parent_children(user_id=request.user.id, club=request.club)


@router.get("/children/{student_id}/", response=ChildProfileOut)
@role_required("parent")
def child_profile(request, student_id: int):
    return get_child_profile(user_id=request.user.id, club=request.club, student_id=student_id)


@router.get("/children/{student_id}/attendance/", response=list[ChildAttendanceOut])
@role_required("parent")
def child_attendance(request, student_id: int, limit: int = 10, offset: int = 0):
    if limit < 1 or limit > 50:
        raise HttpError(400, "limit must be between 1 and 50")
    if offset < 0:
        raise HttpError(400, "offset must be greater than or equal to 0")
    return get_child_attendance(
        user_id=request.user.id,
        club=request.club,
        student_id=student_id,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/children/{student_id}/bank-payment-orders/",
    response={200: BankPaymentOrderOut, 201: BankPaymentOrderOut},
)
@role_required("parent")
def create_child_bank_payment_order(request, student_id: int, payload: SelfServiceBankPaymentOrderCreateIn):
    child = get_parent_child(user_id=request.user.id, club=request.club, student_id=student_id)
    renewal = payload.renewed_from_subscription_id is not None
    if renewal and not is_unified_client_journey_enabled(club=request.club):
        raise HttpError(404, "Contextual renewal is unavailable")
    if renewal and not (payload.idempotency_key or "").strip():
        raise BusinessLogicError(
            "Продление требует стабильный ключ команды",
            code="idempotency_key_required",
        )
    if renewal and (payload.tariff_id is not None or payload.debt_ids):
        raise BusinessLogicError(
            "Точное продление не принимает тариф или произвольный долг",
            code="renewal_client_terms_forbidden",
        )
    if (
        not renewal
        and is_unified_client_journey_enabled(club=request.club)
        and payload.tariff_id is not None
        and Subscription.objects.for_club(request.club)
        .filter(
            student_id=child.id,
            tariff_id=payload.tariff_id,
            deleted_at__isnull=True,
        )
        .exists()
    ):
        raise BusinessLogicError(
            "Контекстное продление требует точный исходный абонемент",
            code="renewal_source_required",
        )
    if not renewal and payload.tariff_id is None:
        raise BusinessLogicError("Укажите тариф", code="tariff_required")
    order = create_bank_payment_order(
        club_id=request.club.id,
        student_id=child.id,
        tariff_id=payload.tariff_id,
        source="parent",
        created_by_id=request.user.id,
        discount_ids=[],
        debt_ids=payload.debt_ids,
        buyer_email=payload.buyer_email,
        buyer_phone=payload.buyer_phone,
        allow_new_self_service_subscription=renewal,
        command_idempotency_key=payload.idempotency_key,
        renewed_from_subscription_id=payload.renewed_from_subscription_id,
        expected_target_tariff_id=payload.expected_target_tariff_id,
        expected_target_price=payload.expected_target_price,
    )
    return (
        200 if getattr(order, "_command_replayed", False) else 201
    ), _mark_bank_payment_order_cancel_scope([order], allowed_sources={"parent"})[0]


@router.get("/children/{student_id}/bank-payment-orders/", response=list[BankPaymentOrderOut])
@role_required("parent")
def child_bank_payment_orders(request, student_id: int, status: str | None = None):
    child = get_parent_child(user_id=request.user.id, club=request.club, student_id=student_id)
    return _mark_bank_payment_order_cancel_scope(
        get_bank_payment_orders(club=request.club, student_id=child.id, status=status),
        allowed_sources={"parent"},
    )


@router.get("/children/{student_id}/bank-payment-orders/{order_id}/", response=BankPaymentOrderOut)
@role_required("parent", conceal_denial=True)
def child_bank_payment_order_detail(request, student_id: int, order_id: int):
    child = get_parent_child(user_id=request.user.id, club=request.club, student_id=student_id)
    order = get_bank_payment_orders(club=request.club, student_id=child.id).filter(id=order_id).first()
    if order is None:
        raise HttpError(404, "Not found")
    return _mark_bank_payment_order_cancel_scope([order], allowed_sources={"parent"})[0]


@router.post("/children/{student_id}/bank-payment-orders/{order_id}/refresh/", response=BankPaymentOrderOut)
@role_required("parent", conceal_denial=True)
def refresh_child_bank_payment_order(request, student_id: int, order_id: int):
    """Coalesce a local status-refresh request without direct provider I/O."""

    child = get_parent_child(user_id=request.user.id, club=request.club, student_id=student_id)
    order = get_bank_payment_orders(club=request.club, student_id=child.id).filter(
        id=order_id,
    ).first()
    if order is None:
        raise HttpError(404, "Not found")
    from apps.billing.service_modules.provider_events import request_provider_reconciliation

    request_provider_reconciliation(
        club_id=request.club.id,
        order_id=order.id,
        provider_event_id=None,
    )
    order.refresh_from_db()
    return _mark_bank_payment_order_cancel_scope([order], allowed_sources={"parent"})[0]


@router.post("/children/{student_id}/bank-payment-orders/{order_id}/cancel/", response=BankPaymentOrderOut)
@role_required("parent", conceal_denial=True)
def cancel_child_bank_payment_order(request, student_id: int, order_id: int):
    child = get_parent_child(user_id=request.user.id, club=request.club, student_id=student_id)
    if not get_bank_payment_orders(club=request.club, student_id=child.id).filter(
        id=order_id,
        source="parent",
    ).exists():
        raise HttpError(404, "Not found")
    order = cancel_bank_payment_order(
        club_id=request.club.id,
        order_id=order_id,
        actor_user_id=request.user.id,
        allowed_student_id=child.id,
        allowed_sources={"parent"},
    )
    return _mark_bank_payment_order_cancel_scope([order], allowed_sources={"parent"})[0]


@router.get("/children/{student_id}/feedback/form/", response={200: FormOut | None})
@role_required("parent")
def child_feedback_form(request, student_id: int):
    get_parent_child(user_id=request.user.id, club=request.club, student_id=student_id)
    return 200, get_active_form(club=request.club)


@router.post(
    "/children/{student_id}/feedback/submit/",
    response={200: SelfServiceResponseOut, 201: SelfServiceResponseOut},
)
@role_required("parent")
def submit_child_feedback(request, student_id: int, payload: SubmitSelfResponseIn):
    child = get_parent_child(user_id=request.user.id, club=request.club, student_id=student_id)
    response = submit_feedback_response(
        club_id=request.club.id,
        form_id=payload.form_id,
        student_id=child.id,
        answers=[
            {
                "question_id": answer.question_id,
                "rating_value": answer.rating_value,
                "bool_value": answer.bool_value,
                "text_value": answer.text_value,
            }
            for answer in payload.answers
        ],
    )
    already_submitted = not getattr(response, "_created", True)
    response.already_submitted = already_submitted
    return (200 if already_submitted else 201), response


@router.post("/invite/", response={201: ParentInviteOut})
@role_required("owner", "admin")
def create_invite(request, payload: CreateInviteIn):
    invite = create_parent_invite(club_id=request.club.id, student_id=payload.student_id)
    return 201, {
        "token": invite.token,
        "student_id": invite.student_id,
        "student_name": str(invite.student),
        "expires_at": invite.expires_at,
    }


@router.post("/accept-invite/", response=AcceptInviteOut)
def accept_invite(request, payload: AcceptInviteIn):
    student = accept_parent_invite(token=payload.token, user_id=request.user.id)
    access_token = None
    next_refresh_token = None
    refresh_token = request.COOKIES.get(auth_tokens.REFRESH_COOKIE_NAME)
    if refresh_token:
        try:
            access_token, next_refresh_token = auth_tokens.switch_refresh_token_membership(
                refresh_token=refresh_token,
                user_id=request.user.id,
                club_id=student.club_id,
                role=ClubMembership.Role.PARENT,
            )
        except auth_tokens.InvalidRefreshTokenError:
            access_token = None

    response = JsonResponse(
        AcceptInviteOut(
            student_id=student.id,
            student_name=str(student),
            club_id=student.club_id,
            club_name=student.club.name,
            access_token=access_token,
        ).dict()
    )
    if next_refresh_token:
        auth_tokens.set_refresh_cookie(response, next_refresh_token)
    elif refresh_token:
        auth_tokens.clear_refresh_cookie(response)
    return response
