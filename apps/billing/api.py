from datetime import date

from django.conf import settings
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from ninja import Query, Router
from ninja.errors import HttpError
from ninja.pagination import LimitOffsetPagination, paginate

from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentProviderEvent,
    Payment,
    ProviderWebhookDelivery,
    Subscription,
)
from apps.billing.schemas import (
    BankPaymentOrderCreateIn,
    BankPaymentOrderOut,
    BankPaymentOrderReviewIn,
    BankPaymentWebhookOut,
    ClubSettingsIn,
    ClubSettingsOut,
    DebtorFilters,
    DebtOut,
    DebtWriteOffIn,
    DeferredProviderEventReplayOut,
    DiscountIn,
    DiscountOut,
    DiscountUpdate,
    ExpenseIn,
    ExpenseOut,
    ExpenseUpdate,
    FreezeIn,
    FreezeOut,
    FreezeRejectIn,
    GroupEnrollmentOptionOut,
    GroupSaleBankOrderV2In,
    GroupSaleCommandOut,
    GroupSaleManualV2In,
    GroupSaleOfferPreviewOut,
    ManualSubscriptionRenewalIn,
    PaymentCapabilitiesOut,
    PaymentIn,
    PaymentOut,
    PaymentRefundApproveIn,
    PaymentRefundCaseOut,
    PaymentRefundOut,
    PaymentRefundPayrollIn,
    PaymentReturnExchangeIn,
    PaymentVerifyIn,
    SubscriptionIn,
    SubscriptionOut,
    TariffIn,
    TariffOut,
    TariffUpdate,
    TrainingTypeIn,
    TrainingTypeOut,
)
from apps.billing.selectors import (
    export_debtors_excel,
    get_active_discounts,
    get_active_training_types,
    get_bank_payment_order_by_id,
    get_bank_payment_orders,
    get_club_subscriptions,
    get_debtors,
    get_expenses,
    get_group_enrollment_options,
    get_open_debts,
    get_open_payment_refund_cases,
    get_payment_by_id,
    get_payments,
    get_student_subscriptions,
    get_subscription_by_id,
    get_subscription_freezes,
    get_tariffs,
)
from apps.billing.service_modules.renewals import (
    build_subscription_command_fingerprint,
    create_manual_subscription_renewal,
)
from apps.billing.services import (
    approve_freeze,
    cancel_bank_payment_order,
    create_bank_payment_order,
    create_discount,
    create_expense,
    create_payment,
    create_subscription,
    create_tariff,
    create_training_type,
    create_v2_group_sale_bank_order,
    create_v2_group_sale_manual,
    delete_expense,
    freeze_subscription,
    get_or_create_club_settings,
    process_bank_payment_webhook,
    reject_freeze,
    replay_deferred_bank_payment_provider_events,
    resolve_bank_payment_order_manual_review,
    resolve_v2_group_sale_offer,
    unfreeze_subscription,
    update_discount,
    update_expense,
    update_tariff,
    verify_payment,
    write_off_debt,
)
from apps.billing.services import (
    update_club_settings as update_club_settings_service,
)
from apps.clubs.capabilities import (
    get_commercial_journey_capability,
    get_v1_commercial_journey_command_availability,
    is_unified_client_journey_enabled,
)
from apps.clubs.models import ClubMembership, ClubSettings
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import role_required
from apps.students.scopes import actor_can_manage_student_sensitive_actions, assert_actor_is_scoped_to_student

router = Router(tags=["billing"])


def _assert_current_staff_membership(request, *, roles: tuple[str, ...]) -> None:
    """Recheck live tenant membership before returning a financial receipt."""

    membership = getattr(request, "_membership", None)
    if membership is None or not ClubMembership.objects.filter(
        id=membership.id,
        club_id=request.club.id,
        user_id=request.user.id,
        is_active=True,
        role__in=roles,
    ).exists():
        raise HttpError(403, "Access denied: current club membership is required")


def _assert_v1_contextual_group_payment_allowed(
    *,
    club,
    idempotency_key: str | None,
    lock_settings: bool = False,
) -> None:
    """Reject a new legacy contextual sale before finance artifacts exist.

    A persisted command key is an immutable replay/drain receipt. Its
    fingerprint remains verified by ``create_payment`` below; this narrow
    preflight merely keeps a tenant protocol flip from blocking that safe
    read/return path.
    """

    command_key = (idempotency_key or "").strip()
    if lock_settings:
        ClubSettings.objects.select_for_update(of=("self",)).filter(
            club_id=club.id,
        ).first()
    capability = get_commercial_journey_capability(club=club)
    if (
        capability.protocol_version != "v2"
        and not capability.unified_client_journey_enabled
    ):
        return
    replay_or_drain = bool(command_key) and Payment.objects.for_club(club).filter(
        command_idempotency_key=command_key
    ).exists()
    availability = get_v1_commercial_journey_command_availability(
        capability=capability,
        accepted_replay_or_drain=replay_or_drain,
    )
    if not availability.allows_new_command:
        raise BusinessLogicError(
            "Commercial journey command is unavailable for this client or tenant.",
            code=availability.code,
        )


def _annotate_staff_bank_payment_orders(orders, *, allowed_sources: set[str] | None):
    order_list = list(orders)
    for order in order_list:
        order.payment_action_mode = "staff"
        order.can_cancel_source_allowed = allowed_sources is None or order.source in allowed_sources
        order.can_refresh_source_allowed = allowed_sources is None or order.source in allowed_sources
    return order_list


def _group_sale_command_result(*, payment, order=None) -> dict:
    """Map persisted commercial facts to the additive v2 UI result contract."""

    student = payment.student
    workspace_state = (
        "student"
        if student.lead_status is None and student.became_student_at is not None
        else "lead"
    )
    if payment.status == Payment.Status.PENDING:
        finance_state = "provider_pending" if order is not None else "pending_manual"
    elif payment.status == Payment.Status.CONFIRMED:
        finance_state = "confirmed"
    else:
        finance_state = "rejected"
    if order is not None:
        order_status = order.status
        if order.status == BankPaymentOrder.Status.CANCELLED:
            finance_state = "cancelled"
        elif order.status == BankPaymentOrder.Status.EXPIRED:
            finance_state = "expired"
        elif order.status == BankPaymentOrder.Status.FAILED:
            finance_state = "failed"
        allowed_actions = [
            action
            for action, enabled in (
                ("cancel", bool(getattr(order, "can_cancel", False))),
                ("refresh", bool(getattr(order, "can_request_refresh", False))),
            )
            if enabled
        ]
    else:
        order_status = ""
        allowed_actions = []
    return {
        "payment_id": payment.id,
        "subscription_id": payment.subscription_id,
        "bank_payment_order_id": order.id if order is not None else None,
        "workspace_state": workspace_state,
        "finance_state": finance_state,
        "payment_status": payment.status,
        "bank_payment_order_status": order_status,
        "fulfillment_state": "fulfilled" if workspace_state == "student" else "pending",
        "provider_payment_url": order.provider_payment_url if order is not None else "",
        "allowed_actions": allowed_actions,
        "command_replayed": bool(
            getattr(payment, "_command_replayed", False)
            or getattr(order, "_command_replayed", False)
        ),
    }


@router.post("/payment-returns/exchange/", auth=None)
def exchange_payment_return(request, payload: PaymentReturnExchangeIn):
    from apps.billing.service_modules.payment_returns import (
        COOKIE_NAME,
        COOKIE_PATH,
        exchange_return_state,
    )

    result = exchange_return_state(
        raw_state=payload.state,
        browser_binding=payload.browser_binding,
        cookie_handle=request.COOKIES.get(COOKIE_NAME),
    )
    if result is None:
        raise HttpError(404, "payment return unavailable")
    handle, projection = result
    response = JsonResponse(projection)
    response.set_cookie(
        COOKIE_NAME,
        handle,
        max_age=30 * 60,
        secure=True,
        httponly=True,
        samesite="Lax",
        path=COOKIE_PATH,
    )
    return response


@router.get("/payment-returns/status/", auth=None)
def payment_return_status(request):
    from apps.billing.service_modules.payment_returns import COOKIE_NAME, projection_for_session

    projection = projection_for_session(cookie_handle=request.COOKIES.get(COOKIE_NAME))
    if projection is None:
        raise HttpError(404, "payment return unavailable")
    return projection


@router.delete("/payment-returns/session/", auth=None, response={204: None})
def clear_payment_return_session(request):
    from apps.billing.service_modules.payment_returns import COOKIE_NAME, COOKIE_PATH, clear_return_session

    clear_return_session(cookie_handle=request.COOKIES.get(COOKIE_NAME))
    response = HttpResponse(status=204)
    response.delete_cookie(COOKIE_NAME, path=COOKIE_PATH)
    return response


@router.get("/payment-capabilities/", response=PaymentCapabilitiesOut)
@role_required("owner", "admin", "trainer", "student", "parent")
def get_payment_capabilities(request):
    from apps.attendance.training_group_selectors import (
        get_training_group_payment_selection_capability,
    )
    from apps.billing.service_modules.payment_readiness import get_online_payment_capability

    capability = get_online_payment_capability()
    is_staff = request._membership.role in {"owner", "admin", "trainer"}
    return PaymentCapabilitiesOut(
        online_payments_enabled=capability.enabled,
        payment_modes=list(capability.payment_modes),
        payment_creation_enabled=capability.creation_enabled,
        payment_reconciliation_enabled=capability.reconciliation_enabled,
        payment_unavailable_reason=(
            capability.reason_code if is_staff else ("unavailable" if not capability.enabled else "")
        ),
        can_create_payment_order=capability.enabled,
        can_request_payment_reconciliation=(
            is_staff and capability.reconciliation_available
        ),
        **get_training_group_payment_selection_capability(club=request.club),
    )


# --- Training Types ---


@router.post("/training-types/", response={201: TrainingTypeOut})
@role_required("owner", "admin")
def create_training_type_endpoint(request, payload: TrainingTypeIn):
    tt = create_training_type(
        club_id=request.club.id,
        name=payload.name,
        slug=payload.slug,
        kind=payload.kind,
        grade_system_id=payload.grade_system_id,
        drop_in_price=payload.drop_in_price,
    )
    return 201, tt


@router.get("/training-types/", response=list[TrainingTypeOut])
@role_required("owner", "admin", "trainer")
def list_training_types(request):
    return list(get_active_training_types(club=request.club))


# --- Tariffs ---


def _tariff_component_payloads(components) -> list[dict] | None:
    if components is None:
        return None
    return [
        {
            "name": component.name,
            "training_type_id": component.training_type_id,
            "entitlement_kind": component.entitlement_kind,
            "credits_total": component.credits_total,
            "weekly_limit": component.weekly_limit,
            "scope": component.scope,
            "location_id": component.location_id,
            "trainer_payout_policy": component.trainer_payout_policy,
            "paid_amount_basis": component.paid_amount_basis,
        }
        for component in components
    ]


def _with_internal_tariff_contract(request, tariffs):
    if request._membership.role not in {"owner", "admin"}:
        return tariffs
    if isinstance(tariffs, list):
        for tariff in tariffs:
            tariff._expose_internal_contract = True
        return tariffs
    tariffs._expose_internal_contract = True
    return tariffs


@router.post("/tariffs/", response={201: TariffOut})
@role_required("owner", "admin")
def create_tariff_endpoint(request, payload: TariffIn):
    tariff = create_tariff(
        club_id=request.club.id,
        name=payload.name,
        training_type_id=payload.training_type_id,
        price=payload.price,
        trainings_limit=payload.trainings_limit,
        duration_days=payload.duration_days,
        scope=payload.scope,
        location_id=payload.location_id,
        description=payload.description,
        trainer_payout_policy=payload.trainer_payout_policy or "",
        components=_tariff_component_payloads(payload.components),
        personal_booking_trainer_id=payload.personal_booking_trainer_id,
        is_personal_booking_default=payload.is_personal_booking_default,
    )
    return 201, _with_internal_tariff_contract(request, tariff)


@router.get("/tariffs/", response=list[TariffOut])
@role_required("owner", "admin", "trainer")
@paginate(LimitOffsetPagination)
def list_tariffs(request):
    tariffs = list(get_tariffs(club=request.club))
    return _with_internal_tariff_contract(request, tariffs)


@router.put("/tariffs/{tariff_id}/", response=TariffOut)
@role_required("owner", "admin")
def update_tariff_endpoint(request, tariff_id: int, payload: TariffUpdate):
    fields = {}
    for field_name in (
        "name",
        "price",
        "trainings_limit",
        "duration_days",
        "is_active",
        "description",
        "trainer_payout_policy",
        "personal_booking_trainer_id",
        "is_personal_booking_default",
    ):
        if field_name in payload.model_fields_set:
            fields[field_name] = getattr(payload, field_name)
    tariff = update_tariff(
        tariff_id=tariff_id,
        club_id=request.club.id,
        components=(
            _tariff_component_payloads(payload.components)
            if "components" in payload.model_fields_set
            else None
        ),
        is_personal_booking_default=fields.pop("is_personal_booking_default", None),
        **fields,
    )
    return _with_internal_tariff_contract(request, tariff)


# --- Subscriptions ---


@router.post("/subscriptions/", response={201: SubscriptionOut})
@role_required("owner", "admin")
def create_subscription_endpoint(request, payload: SubscriptionIn):
    sub = create_subscription(
        club_id=request.club.id,
        recorded_by_id=request.user.id,
        student_id=payload.student_id,
        tariff_id=payload.tariff_id,
        payment_method=payload.payment_method,
        package_owner_trainer_id=payload.package_owner_trainer_id,
        debt_ids=payload.debt_ids,
    )
    return 201, get_subscription_by_id(club=request.club, subscription_id=sub.id)


@router.get("/subscriptions/", response=list[SubscriptionOut])
@role_required("owner", "admin", "trainer")
@paginate(LimitOffsetPagination)
def list_subscriptions(
    request,
    student_id: int = Query(None),
    booking_date: date | None = Query(None),
):
    if request._membership.role == "trainer":
        if student_id is None:
            raise HttpError(403, "student_id required for trainer subscription lookup")
        _assert_trainer_billing_student_scope(request, student_id=student_id)
    if student_id:
        return get_student_subscriptions(
            club=request.club,
            student_id=student_id,
            booking_date=booking_date,
        )
    return get_club_subscriptions(club=request.club, booking_date=booking_date)


@router.get("/subscriptions/{subscription_id}/", response=SubscriptionOut)
@role_required("owner", "admin", "trainer")
def get_subscription_detail(request, subscription_id: int):
    subscription = get_subscription_by_id(club=request.club, subscription_id=subscription_id)
    if request._membership.role == "trainer":
        _assert_trainer_billing_student_scope(request, student_id=subscription.student_id)
    return subscription


# --- Payments ---


def _get_current_trainer_id(request) -> int:
    from apps.trainers.models import Trainer
    from apps.trainers.selectors import get_trainer_for_user

    try:
        trainer = get_trainer_for_user(club=request.club, user=request.user)
    except Trainer.DoesNotExist:
        raise HttpError(403, "Trainer profile not found")
    return trainer.id


def _assert_trainer_payment_student_scope(request, *, trainer_id: int, student_id: int) -> None:
    if not actor_can_manage_student_sensitive_actions(
        club=request.club,
        membership_role=request._membership.role,
        user=request.user,
        student_id=student_id,
    ):
        raise HttpError(403, "Student is not available for trainer payment")


def _assert_trainer_billing_student_scope(request, *, student_id: int) -> None:
    assert_actor_is_scoped_to_student(
        club=request.club,
        membership_role=request._membership.role,
        user=request.user,
        student_id=student_id,
    )


def _assert_trainer_billing_management_student_scope(request, *, student_id: int) -> None:
    if not actor_can_manage_student_sensitive_actions(
        club=request.club,
        membership_role=request._membership.role,
        user=request.user,
        student_id=student_id,
    ):
        raise HttpError(403, "Access denied: not your student")


def _resolve_payment_trainer_ids(
    request,
    payload: PaymentIn | BankPaymentOrderCreateIn,
) -> tuple[int | None, int | None]:
    if request._membership.role != "trainer":
        return payload.seller_trainer_id, payload.package_owner_trainer_id

    trainer_id = _get_current_trainer_id(request)
    _assert_trainer_payment_student_scope(request, trainer_id=trainer_id, student_id=payload.student_id)
    if payload.target_schedule_id is not None:
        return None, None
    return trainer_id, trainer_id


def _bank_payment_source_for_role(role: str) -> str:
    if role == "trainer":
        return BankPaymentOrder.Source.TRAINER
    if role == "admin":
        return BankPaymentOrder.Source.ADMIN
    return BankPaymentOrder.Source.OWNER


def _bank_payment_allowed_sources_for_role(role: str) -> set[str] | None:
    if role == "trainer":
        return {BankPaymentOrder.Source.TRAINER}
    return None


def _trainer_booking_linked_order_filter(*, trainer_id: int) -> Q:
    return (
        Q(personal_drop_in_payment_link__isnull=True)
        | Q(personal_drop_in_payment_link__booking__enrollment__schedule__trainer_id=trainer_id)
    ) & (
        Q(personal_payment_reservation__isnull=True)
        | Q(personal_payment_reservation__trainer_id=trainer_id)
    )


def _assert_trainer_booking_linked_order_scope(request, *, order_id: int, trainer_id: int) -> None:
    from apps.attendance.models import PersonalBookingPaymentReservation, PersonalDropInPaymentLink

    foreign_drop_in_order = (
        PersonalDropInPaymentLink.objects.for_club(request.club)
        .filter(bank_payment_order_id=order_id)
        .exclude(booking__enrollment__schedule__trainer_id=trainer_id)
        .exists()
    )
    foreign_reservation_order = (
        PersonalBookingPaymentReservation.objects.for_club(request.club)
        .filter(bank_payment_order_id=order_id)
        .exclude(trainer_id=trainer_id)
        .exists()
    )
    if foreign_drop_in_order or foreign_reservation_order:
        raise HttpError(403, "Bank payment order belongs to another trainer booking")


def _get_concealed_bank_payment_order(*, club, order_id: int) -> BankPaymentOrder:
    try:
        return get_bank_payment_order_by_id(club=club, order_id=order_id)
    except BankPaymentOrder.DoesNotExist as exc:
        raise HttpError(404, "Bank payment order not found") from exc


def _assert_trainer_exact_bank_order_scope(request, *, order: BankPaymentOrder) -> None:
    try:
        _assert_trainer_billing_student_scope(request, student_id=order.student_id)
        trainer_id = _get_current_trainer_id(request)
        _assert_trainer_booking_linked_order_scope(
            request,
            order_id=order.id,
            trainer_id=trainer_id,
        )
    except HttpError as exc:
        raise HttpError(404, "Bank payment order not found") from exc
    if order.source != BankPaymentOrder.Source.TRAINER:
        raise HttpError(404, "Bank payment order not found")


@router.get("/group-enrollment-options/", response=list[GroupEnrollmentOptionOut])
@role_required("owner", "admin", "trainer")
def list_group_enrollment_options(
    request,
    student_id: int,
    tariff_id: int,
):
    enforce_trainer_contract = request._membership.role == "trainer"
    if enforce_trainer_contract:
        trainer_id = _get_current_trainer_id(request)
        _assert_trainer_payment_student_scope(
            request,
            trainer_id=trainer_id,
            student_id=student_id,
        )
    from apps.attendance.training_group_selectors import (
        get_training_group_payment_selection_capability,
    )

    capability = get_training_group_payment_selection_capability(club=request.club)
    if capability["training_group_payment_selection_mode"] == "disabled":
        return []
    return get_group_enrollment_options(
        club=request.club,
        student_id=student_id,
        tariff_id=tariff_id,
        enforce_trainer_contract=enforce_trainer_contract,
        canonical_cards_enabled=capability["canonical_group_selection_enabled"],
    )


@router.get("/group-sale-offers/preview/", response=GroupSaleOfferPreviewOut)
@role_required("owner", "admin", "trainer")
def preview_group_sale_offer(
    request,
    student_id: int,
    tariff_id: int,
    target_training_group_id: int,
    target_schedule_id: int,
    target_start_date: date,
):
    """Return the one v2 offer that both strict group command routes consume."""

    if request._membership.role == "trainer":
        _assert_trainer_payment_student_scope(
            request,
            trainer_id=_get_current_trainer_id(request),
            student_id=student_id,
        )
    return resolve_v2_group_sale_offer(
        club_id=request.club.id,
        student_id=student_id,
        tariff_id=tariff_id,
        target_training_group_id=target_training_group_id,
        target_schedule_id=target_schedule_id,
        target_start_date=target_start_date,
        lock=False,
    ).payload


@router.post(
    "/v2/group-sales/manual/",
    response={200: GroupSaleCommandOut, 201: GroupSaleCommandOut},
)
@role_required("owner", "admin", "trainer")
def create_v2_group_sale_manual_endpoint(request, payload: GroupSaleManualV2In):
    if request._membership.role == "trainer":
        _assert_trainer_payment_student_scope(
            request,
            trainer_id=_get_current_trainer_id(request),
            student_id=payload.student_id,
        )
    payment = create_v2_group_sale_manual(
        club_id=request.club.id,
        student_id=payload.student_id,
        tariff_id=payload.tariff_id,
        payment_method=payload.payment_method,
        target_training_group_id=payload.target_training_group_id,
        target_schedule_id=payload.target_schedule_id,
        target_start_date=payload.target_start_date,
        expected_offer_digest=payload.expected_offer_digest,
        command_idempotency_key=payload.idempotency_key,
        recorded_by_id=request.user.id,
        enforce_trainer_group_contract=request._membership.role == "trainer",
    )
    return (
        200 if getattr(payment, "_command_replayed", False) else 201,
        _group_sale_command_result(payment=payment),
    )


@router.post(
    "/v2/group-sales/bank-orders/",
    response={200: GroupSaleCommandOut, 201: GroupSaleCommandOut},
)
@role_required("owner", "admin", "trainer")
def create_v2_group_sale_bank_order_endpoint(request, payload: GroupSaleBankOrderV2In):
    if request._membership.role == "trainer":
        _assert_trainer_payment_student_scope(
            request,
            trainer_id=_get_current_trainer_id(request),
            student_id=payload.student_id,
        )
    order = create_v2_group_sale_bank_order(
        club_id=request.club.id,
        student_id=payload.student_id,
        tariff_id=payload.tariff_id,
        target_training_group_id=payload.target_training_group_id,
        target_schedule_id=payload.target_schedule_id,
        target_start_date=payload.target_start_date,
        expected_offer_digest=payload.expected_offer_digest,
        command_idempotency_key=payload.idempotency_key,
        created_by_id=request.user.id,
        source=_bank_payment_source_for_role(request._membership.role),
        buyer_email=payload.buyer_email,
        enforce_trainer_group_contract=request._membership.role == "trainer",
    )
    _annotate_staff_bank_payment_orders(
        [order],
        allowed_sources=_bank_payment_allowed_sources_for_role(request._membership.role),
    )
    return (
        200 if getattr(order, "_command_replayed", False) else 201,
        _group_sale_command_result(payment=order.payment, order=order),
    )


@router.post("/payments/", response={200: PaymentOut, 201: PaymentOut})
@role_required("owner", "admin", "trainer")
def create_payment_endpoint(request, payload: PaymentIn):
    _assert_current_staff_membership(request, roles=("owner", "admin", "trainer"))
    discount_ids = payload.discount_ids
    if request._membership.role == "trainer" and len(discount_ids) > 1:
        raise BusinessLogicError(
            "Тренер может применить только одну скидку к ручной оплате",
            code="trainer_multiple_discounts_not_allowed",
        )

    seller_trainer_id, package_owner_trainer_id = _resolve_payment_trainer_ids(request, payload)
    capability = get_commercial_journey_capability(club=request.club)
    unified_client_journey_enabled = capability.unified_client_journey_enabled
    contextual_group = payload.target_schedule_id is not None or payload.target_training_group_id is not None
    if (
        contextual_group
        and unified_client_journey_enabled
        and not (payload.idempotency_key or "").strip()
    ):
        raise BusinessLogicError(
            "Контекстная групповая оплата требует стабильный ключ",
            code="idempotency_key_required",
        )
    # A revised renewal names the historical source in ``tariff_id`` and the
    # accepted target in the expected-offer fields.  The payment command must
    # use that target tariff, while same-ID legacy renewals keep their old
    # fingerprint shape for replay compatibility.
    payment_tariff_id = payload.tariff_id
    fingerprint_expected_target_id = None
    fingerprint_expected_price = None
    if (
        payload.renewed_from_subscription_id is not None
        and payload.expected_target_tariff_id is not None
        and payload.expected_target_tariff_id != payload.tariff_id
    ):
        payment_tariff_id = payload.expected_target_tariff_id
        fingerprint_expected_target_id = payload.expected_target_tariff_id
        fingerprint_expected_price = payload.expected_target_price
    command_fingerprint = (
        build_subscription_command_fingerprint(
            student_id=payload.student_id,
            tariff_id=payment_tariff_id,
            payment_method=payload.payment_method,
            discount_ids=discount_ids,
            debt_ids=payload.debt_ids,
            target_schedule_id=payload.target_schedule_id,
            target_training_group_id=payload.target_training_group_id,
            target_start_date=payload.target_start_date,
            renewed_from_subscription_id=payload.renewed_from_subscription_id,
            # The exact schedule/group owns seller attribution; a caller
            # supplied seller is overwritten during canonicalization and must
            # not make the same commercial command conflict on replay.
            seller_trainer_id=(
                None if payload.target_schedule_id is not None else seller_trainer_id
            ),
            package_owner_trainer_id=package_owner_trainer_id,
            expected_target_tariff_id=fingerprint_expected_target_id,
            expected_target_price=fingerprint_expected_price,
        )
        if (payload.idempotency_key or "").strip()
        else None
    )
    if contextual_group:
        _assert_v1_contextual_group_payment_allowed(
            club=request.club,
            idempotency_key=payload.idempotency_key,
        )
    renewal_source_tariff_id = None
    if payload.renewed_from_subscription_id is not None:
        # ``tariff_id`` is the accepted target for a revised generic payment,
        # while the source tariff is carried by the exact subscription. Read
        # that source identity before entering the service; the service still
        # performs the authoritative tenant/student lock and replay lookup.
        renewal_source_tariff_id = (
            Subscription.objects.for_club(request.club)
            .filter(
                id=payload.renewed_from_subscription_id,
                student_id=payload.student_id,
                deleted_at__isnull=True,
            )
            .values_list("tariff_id", flat=True)
            .first()
        )
    payment = create_payment(
        club_id=request.club.id,
        student_id=payload.student_id,
        tariff_id=payment_tariff_id,
        payment_method=payload.payment_method,
        discount_ids=discount_ids,
        debt_ids=payload.debt_ids,
        recorded_by_id=request.user.id,
        seller_trainer_id=seller_trainer_id,
        target_training_group_id=payload.target_training_group_id,
        package_owner_trainer_id=package_owner_trainer_id,
        target_schedule_id=payload.target_schedule_id,
        target_start_date=payload.target_start_date,
        allow_renewal=payload.renewed_from_subscription_id is not None,
        renewed_from_subscription_id=payload.renewed_from_subscription_id,
        renewal_source_tariff_id=renewal_source_tariff_id,
        expected_target_tariff_id=(
            payload.expected_target_tariff_id
            if payload.renewed_from_subscription_id is not None
            else None
        ),
        expected_target_price=(
            payload.expected_target_price
            if payload.renewed_from_subscription_id is not None
            else None
        ),
        enforce_trainer_group_contract=request._membership.role == "trainer",
        create_manual_operational_admission=(
            contextual_group and unified_client_journey_enabled
        ),
        # The service evaluates this policy only after it locks the Student,
        # so a concurrent conversion cannot turn an active package sale into
        # a legacy admission.
        mixed_v1_lead_only_admission=(
            contextual_group
            and capability.protocol_version == ClubSettings.CommercialJourneyProtocol.V1
            and settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED
        ),
        command_idempotency_key=payload.idempotency_key,
        command_fingerprint=command_fingerprint,
        _locked_pre_create_validator=(
            lambda: _assert_v1_contextual_group_payment_allowed(
                club=request.club,
                idempotency_key=payload.idempotency_key,
                lock_settings=True,
            )
            if contextual_group
            else None
        ),
    )
    return (200 if getattr(payment, "_command_replayed", False) else 201), payment


@router.post("/payments/renewals/", response={200: PaymentOut, 201: PaymentOut})
@role_required("owner", "admin", "trainer")
def create_manual_subscription_renewal_endpoint(request, payload: ManualSubscriptionRenewalIn):
    _assert_current_staff_membership(request, roles=("owner", "admin", "trainer"))
    if not is_unified_client_journey_enabled(club=request.club):
        raise HttpError(404, "Contextual renewal is unavailable")
    if request._membership.role == "trainer":
        _assert_trainer_payment_student_scope(
            request,
            trainer_id=_get_current_trainer_id(request),
            student_id=payload.student_id,
        )
        if len(payload.discount_ids) > 1:
            raise BusinessLogicError(
                "Тренер может применить только одну скидку к ручной оплате",
                code="trainer_multiple_discounts_not_allowed",
            )
    payment = create_manual_subscription_renewal(
        club_id=request.club.id,
        student_id=payload.student_id,
        renewed_from_subscription_id=payload.renewed_from_subscription_id,
        payment_method=payload.payment_method,
        recorded_by_id=request.user.id,
        command_idempotency_key=payload.idempotency_key,
        discount_ids=payload.discount_ids,
        expected_target_tariff_id=payload.expected_target_tariff_id,
        expected_target_price=payload.expected_target_price,
    )
    return (200 if getattr(payment, "_command_replayed", False) else 201), payment


@router.get("/payments/", response=list[PaymentOut])
@role_required("owner", "admin")
@paginate(LimitOffsetPagination)
def list_payments(request, student_id: int = Query(None), status: str = Query(None)):
    return get_payments(club=request.club, student_id=student_id, status=status)


@router.get("/payments/{payment_id}/", response=PaymentOut)
@role_required("owner", "admin")
def get_payment_detail(request, payment_id: int):
    return get_payment_by_id(club=request.club, payment_id=payment_id)


@router.post("/payments/{payment_id}/verify/", response=PaymentOut)
@role_required("owner", "admin")
def verify_payment_endpoint(request, payment_id: int, payload: PaymentVerifyIn):
    payment = verify_payment(
        payment_id=payment_id,
        club_id=request.club.id,
        verified_by_id=request.user.id,
        action=payload.action,
        rejection_reason=payload.rejection_reason,
    )
    return payment


@router.post("/bank-payment-orders/", response={200: BankPaymentOrderOut, 201: BankPaymentOrderOut})
@role_required("owner", "admin", "trainer")
def create_bank_payment_order_endpoint(request, payload: BankPaymentOrderCreateIn):
    _assert_current_staff_membership(request, roles=("owner", "admin", "trainer"))
    discount_ids = payload.discount_ids
    if request._membership.role == "trainer" and discount_ids:
        raise BusinessLogicError(
            "Тренер не может применять скидки при создании ссылки на оплату",
            code="trainer_discounts_not_allowed",
        )

    seller_trainer_id, package_owner_trainer_id = _resolve_payment_trainer_ids(request, payload)
    contextual_group = payload.target_schedule_id is not None or payload.target_training_group_id is not None
    contextual_renewal = payload.renewed_from_subscription_id is not None
    if contextual_renewal:
        if payload.debt_ids or discount_ids:
            raise BusinessLogicError(
                "Точное продление не принимает скидку или произвольный долг",
                code="renewal_client_terms_forbidden",
            )
        if contextual_group:
            # A group-renewal receipt preserves the exact membership target.
            # The source still owns tariff/amount; the service revalidates all
            # anchors after Club→Student→source locks.
            if (
                payload.tariff_id is None
                or payload.target_schedule_id is None
                or payload.target_training_group_id is None
                or payload.target_start_date is None
            ):
                raise BusinessLogicError(
                    "Групповое продление требует точную группу, слот и дату старта",
                    code="renewal_group_target_required",
                )
        elif (
            payload.tariff_id is not None
            or payload.target_schedule_id is not None
            or payload.target_training_group_id is not None
            or payload.target_start_date is not None
        ):
            raise BusinessLogicError(
                "Точное продление принимает только источник и контакт покупателя",
                code="renewal_client_terms_forbidden",
            )
    if (
        (contextual_group or contextual_renewal)
        and is_unified_client_journey_enabled(club=request.club)
        and not (payload.idempotency_key or "").strip()
    ):
        raise BusinessLogicError(
            "Контекстная команда оплаты требует стабильный ключ",
            code="idempotency_key_required",
        )
    if contextual_group and not contextual_renewal:
        _assert_v1_contextual_group_payment_allowed(
            club=request.club,
            idempotency_key=payload.idempotency_key,
        )
    order = create_bank_payment_order(
        club_id=request.club.id,
        student_id=payload.student_id,
        tariff_id=payload.tariff_id,
        source=_bank_payment_source_for_role(request._membership.role),
        created_by_id=request.user.id,
        seller_trainer_id=seller_trainer_id,
        package_owner_trainer_id=package_owner_trainer_id,
        discount_ids=discount_ids,
        debt_ids=payload.debt_ids,
        target_schedule_id=payload.target_schedule_id,
        target_start_date=payload.target_start_date,
        target_training_group_id=payload.target_training_group_id,
        buyer_email=payload.buyer_email,
        buyer_phone=payload.buyer_phone,
        enforce_trainer_group_contract=request._membership.role == "trainer",
        command_idempotency_key=payload.idempotency_key,
        renewed_from_subscription_id=payload.renewed_from_subscription_id,
        expected_target_tariff_id=payload.expected_target_tariff_id,
        expected_target_price=payload.expected_target_price,
        _locked_pre_reuse_validator=(
            lambda: _assert_v1_contextual_group_payment_allowed(
                club=request.club,
                idempotency_key=payload.idempotency_key,
                lock_settings=True,
            )
            if contextual_group and not contextual_renewal
            else None
        ),
    )
    return (200 if getattr(order, "_command_replayed", False) else 201), _annotate_staff_bank_payment_orders(
        [order],
        allowed_sources=_bank_payment_allowed_sources_for_role(request._membership.role),
    )[0]


@router.post(
    "/bank-payment-orders/replay-deferred-provider-events/",
    response=DeferredProviderEventReplayOut,
)
@role_required("owner", "admin")
def replay_deferred_provider_events_endpoint(request):
    return replay_deferred_bank_payment_provider_events(club_id=request.club.id)


@router.get("/bank-payment-orders/", response=list[BankPaymentOrderOut])
@role_required("owner", "admin", "trainer")
@paginate(LimitOffsetPagination)
def list_bank_payment_orders(
    request,
    student_id: int | None = Query(None),
    status: str | None = Query(None),
):
    if request._membership.role == "trainer":
        if student_id is None:
            raise HttpError(403, "student_id required for trainer bank payment order lookup")
        _assert_trainer_billing_student_scope(request, student_id=student_id)
        trainer_id = _get_current_trainer_id(request)
    orders = get_bank_payment_orders(club=request.club, student_id=student_id, status=status)
    allowed_sources = _bank_payment_allowed_sources_for_role(request._membership.role)
    if allowed_sources is not None:
        orders = orders.filter(
            source__in=allowed_sources,
        ).filter(_trainer_booking_linked_order_filter(trainer_id=trainer_id))
    return _annotate_staff_bank_payment_orders(orders, allowed_sources=allowed_sources)


@router.get("/bank-payment-orders/{order_id}/", response=BankPaymentOrderOut)
@role_required("owner", "admin", "trainer", conceal_denial=True)
def get_bank_payment_order_detail(request, order_id: int):
    order = _get_concealed_bank_payment_order(club=request.club, order_id=order_id)
    if request._membership.role == "trainer":
        _assert_trainer_exact_bank_order_scope(request, order=order)
    return _annotate_staff_bank_payment_orders(
        [order],
        allowed_sources=_bank_payment_allowed_sources_for_role(request._membership.role),
    )[0]


@router.post("/bank-payment-orders/{order_id}/cancel/", response=BankPaymentOrderOut)
@role_required("owner", "admin", "trainer", conceal_denial=True)
def cancel_bank_payment_order_endpoint(request, order_id: int):
    order = _get_concealed_bank_payment_order(club=request.club, order_id=order_id)
    if request._membership.role == "trainer":
        _assert_trainer_exact_bank_order_scope(request, order=order)
    allowed_sources = _bank_payment_allowed_sources_for_role(request._membership.role)
    order = cancel_bank_payment_order(
        club_id=request.club.id,
        order_id=order_id,
        actor_user_id=request.user.id,
        allowed_sources=allowed_sources,
    )
    return _annotate_staff_bank_payment_orders([order], allowed_sources=allowed_sources)[0]


@router.post("/bank-payment-orders/{order_id}/review/", response=BankPaymentOrderOut)
@role_required("owner", "admin")
def resolve_bank_payment_order_review_endpoint(request, order_id: int, payload: BankPaymentOrderReviewIn):
    order = resolve_bank_payment_order_manual_review(
        club_id=request.club.id,
        order_id=order_id,
        actor_user_id=request.user.id,
        resolution=payload.resolution,
        reason=payload.reason,
        evidence=payload.evidence,
    )
    return _annotate_staff_bank_payment_orders([order], allowed_sources=None)[0]


@router.post("/bank-payment-orders/{order_id}/reconcile/", response=BankPaymentOrderOut)
@role_required("owner", "admin")
def reconcile_bank_payment_order_endpoint(request, order_id: int):
    """Request one cooldown/lease-controlled provider-backed reconciliation."""

    order = get_bank_payment_order_by_id(club=request.club, order_id=order_id)
    from apps.billing.service_modules.provider_events import (
        enqueue_provider_reconciliation,
        request_provider_reconciliation,
    )

    request_provider_reconciliation(
        club_id=request.club.id,
        order_id=order.id,
        provider_event_id=None,
        allow_manual_retry=True,
        actor_user_id=request.user.id,
    )
    enqueue_provider_reconciliation(club_id=request.club.id, order_id=order.id)
    order = get_bank_payment_order_by_id(club=request.club, order_id=order_id)
    return _annotate_staff_bank_payment_orders([order], allowed_sources=None)[0]


@router.post("/bank-payment-orders/{order_id}/refresh/", response=BankPaymentOrderOut)
@role_required("owner", "admin", "trainer", conceal_denial=True)
def refresh_bank_payment_order_endpoint(request, order_id: int):
    """Coalesce a local status-refresh request without direct provider I/O."""

    order = _get_concealed_bank_payment_order(club=request.club, order_id=order_id)
    if request._membership.role == "trainer":
        _assert_trainer_exact_bank_order_scope(request, order=order)
    from apps.billing.service_modules.provider_events import request_provider_reconciliation

    request_provider_reconciliation(
        club_id=request.club.id,
        order_id=order.id,
        provider_event_id=None,
    )
    order = get_bank_payment_order_by_id(club=request.club, order_id=order_id)
    return _annotate_staff_bank_payment_orders(
        [order],
        allowed_sources=_bank_payment_allowed_sources_for_role(request._membership.role),
    )[0]


@router.get("/payment-refund-cases/", response=list[PaymentRefundCaseOut])
@role_required("owner", "admin")
def list_payment_refund_cases(request):
    return list(get_open_payment_refund_cases(club=request.club))


@router.post("/payment-refund-cases/{case_id}/approve/", response=PaymentRefundOut)
@role_required("owner", "admin")
def approve_payment_refund_case_endpoint(
    request,
    case_id: int,
    payload: PaymentRefundApproveIn,
):
    from apps.billing.refund_services import approve_payment_refund_case

    return approve_payment_refund_case(
        club_id=request.club.id,
        case_id=case_id,
        actor_user_id=request.user.id,
        idempotency_key=payload.idempotency_key,
        amount=payload.amount,
        refund_kind=payload.refund_kind,
        reason=payload.reason,
        entitlement_action=payload.entitlement_action,
        legacy_enrollment_action=payload.legacy_enrollment_action,
        legacy_enrollment_id=payload.legacy_enrollment_id,
    )


@router.post("/payment-refunds/{refund_id}/complete-payroll/", response=PaymentRefundOut)
@role_required("owner", "admin")
def complete_payment_refund_payroll_endpoint(
    request,
    refund_id: int,
    payload: PaymentRefundPayrollIn,
):
    from apps.billing.refund_services import complete_payment_refund_payroll

    return complete_payment_refund_payroll(
        club_id=request.club.id,
        refund_id=refund_id,
        actor_user_id=request.user.id,
        effective_date=payload.effective_date,
    )


@router.post("/payment-provider-webhooks/{provider}/", auth=None, response=BankPaymentWebhookOut)
def payment_provider_webhook(request, provider: str):
    max_body_bytes = int(settings.TOCHKA_WEBHOOK_MAX_BODY_BYTES)
    if not 1024 <= max_body_bytes <= 65536:
        raise BusinessLogicError(
            "Настройки webhook оплаты некорректны",
            code="invalid_payment_webhook",
        )
    content_length = request.headers.get("Content-Length")
    try:
        if content_length is not None:
            declared_length = int(content_length)
            if declared_length < 0 or declared_length > max_body_bytes:
                raise BusinessLogicError(
                    "Некорректный webhook оплаты",
                    code="invalid_payment_webhook",
                )
    except ValueError as exc:
        raise BusinessLogicError(
            "Некорректный webhook оплаты",
            code="invalid_payment_webhook",
        ) from exc

    request_body = request.read(max_body_bytes + 1)
    if not isinstance(request_body, bytes):
        # Django's real HttpRequest.read() always returns bytes. Django Ninja's
        # in-process test transport stores its synthetic body directly on the
        # request mock, so use that already-materialized test value without
        # touching the real HttpRequest.body property.
        request_body = request.__dict__.get("body", b"")
        if isinstance(request_body, str):
            request_body = request_body.encode("utf-8")
    if not request_body or len(request_body) > max_body_bytes:
        raise BusinessLogicError(
            "Некорректный webhook оплаты",
            code="invalid_payment_webhook",
        )

    result = process_bank_payment_webhook(
        provider=provider,
        request_body=request_body,
        headers=request.headers,
        request_id=request.headers.get("X-Request-ID", ""),
    )
    if isinstance(result, ProviderWebhookDelivery):
        return BankPaymentWebhookOut(
            event_id=None,
            delivery_id=result.id,
            order_id=None,
            processing_status=result.outcome,
            provider_status=result.provider_status,
        )

    if not isinstance(result, BankPaymentProviderEvent):
        raise BusinessLogicError(
            "Некорректный результат обработки webhook оплаты",
            code="invalid_payment_webhook_result",
        )
    delivery = result.global_delivery
    return BankPaymentWebhookOut(
        event_id=result.id,
        delivery_id=delivery.id,
        order_id=result.order_id,
        processing_status=result.processing_status,
        provider_status=result.provider_status,
    )


# --- Discounts ---


@router.post("/discounts/", response={201: DiscountOut})
@role_required("owner", "admin")
def create_discount_endpoint(request, payload: DiscountIn):
    discount = create_discount(
        club_id=request.club.id,
        name=payload.name,
        discount_type=payload.discount_type,
        value=payload.value,
    )
    return 201, discount


@router.get("/discounts/", response=list[DiscountOut])
@role_required("owner", "admin", "trainer")
def list_discounts(request):
    return list(get_active_discounts(club=request.club))


@router.put("/discounts/{discount_id}/", response=DiscountOut)
@role_required("owner", "admin")
def update_discount_endpoint(request, discount_id: int, payload: DiscountUpdate):
    fields = {}
    for field_name in ("name", "value", "is_active"):
        if field_name in payload.model_fields_set:
            fields[field_name] = getattr(payload, field_name)
    return update_discount(
        discount_id=discount_id,
        club_id=request.club.id,
        **fields,
    )


# --- Debts ---


@router.post("/debts/{debt_id}/write-off/", response=DebtOut)
@role_required("owner", "admin")
def write_off_debt_endpoint(request, debt_id: int, payload: DebtWriteOffIn | None = None):
    return write_off_debt(
        debt_id=debt_id,
        club_id=request.club.id,
        written_off_by_id=request.user.id,
        reason=payload.reason if payload is not None else "",
    )


@router.get("/debts/", response=list[DebtOut])
@role_required("owner", "admin", "trainer")
@paginate(LimitOffsetPagination)
def list_debts(request, student_id: int | None = Query(None)):
    if request._membership.role == "trainer" and student_id is None:
        raise HttpError(403, "student_id required for trainer debt lookup")
    if request._membership.role == "trainer" and student_id is not None:
        _assert_trainer_billing_student_scope(request, student_id=student_id)
    return get_open_debts(club=request.club, student_id=student_id)


# --- Freeze ---


@router.post("/subscriptions/{subscription_id}/freeze/", response={201: FreezeOut})
@role_required("owner", "admin", "trainer")
def freeze_subscription_endpoint(request, subscription_id: int, payload: FreezeIn):
    subscription = get_subscription_by_id(club=request.club, subscription_id=subscription_id)
    if request._membership.role == "trainer":
        _assert_trainer_billing_management_student_scope(request, student_id=subscription.student_id)
    freeze = freeze_subscription(
        club_id=request.club.id,
        subscription_id=subscription_id,
        days=payload.days,
        reason=payload.reason,
        frozen_by_id=request.user.id,
        initiator_role=request._membership.role,
    )
    return 201, freeze


@router.patch("/freezes/{freeze_id}/approve/", response=FreezeOut)
@role_required("owner", "admin")
def approve_freeze_endpoint(request, freeze_id: int):
    return approve_freeze(
        freeze_id=freeze_id,
        club_id=request.club.id,
        approved_by_id=request.user.id,
    )


@router.patch("/freezes/{freeze_id}/reject/", response=FreezeOut)
@role_required("owner", "admin")
def reject_freeze_endpoint(request, freeze_id: int, payload: FreezeRejectIn | None = None):
    return reject_freeze(
        freeze_id=freeze_id,
        club_id=request.club.id,
        rejected_by_id=request.user.id,
        decision_reason=payload.decision_reason if payload is not None else "",
    )


@router.post("/freezes/{freeze_id}/unfreeze/", response=FreezeOut)
@role_required("owner", "admin")
def unfreeze_subscription_endpoint(request, freeze_id: int):
    return unfreeze_subscription(freeze_id=freeze_id, club_id=request.club.id)


@router.get("/subscriptions/{subscription_id}/freezes/", response=list[FreezeOut])
@role_required("owner", "admin", "trainer")
def list_subscription_freezes(request, subscription_id: int):
    subscription = get_subscription_by_id(club=request.club, subscription_id=subscription_id)
    if request._membership.role == "trainer":
        _assert_trainer_billing_student_scope(request, student_id=subscription.student_id)
    return list(get_subscription_freezes(club=request.club, subscription_id=subscription_id))


# --- Debtors ---


@router.get("/debtors/", response=list[DebtOut])
@role_required("owner", "admin")
@paginate(LimitOffsetPagination)
def list_debtors(request, filters: DebtorFilters = Query(...)):
    return get_debtors(club=request.club, **filters.dict(exclude_unset=True))


@router.get("/debtors/export/")
@role_required("owner", "admin")
def export_debtors(request, filters: DebtorFilters = Query(...)):
    data = export_debtors_excel(club=request.club, **filters.dict(exclude_unset=True))
    response = HttpResponse(
        data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="debtors.xlsx"'
    return response


# --- Club Settings ---


def _club_settings_payload(request, settings):
    # Resolve logo: uploaded file takes priority, return absolute URL for PWA.
    # Build a dict to avoid mutating the live ORM instance (which could be
    # persisted back by a signal/middleware).
    return {
        "freeze_enabled": settings.freeze_enabled,
        "freeze_max_days": settings.freeze_max_days,
        "freeze_max_count": settings.freeze_max_count,
        "timezone": request.club.timezone,
        "primary_color": settings.primary_color,
        "accent_color": settings.accent_color,
        "club_name_display": settings.club_name_display,
        "logo_url": (
            request.build_absolute_uri(settings.logo_file.url)
            if settings.logo_file else settings.logo_url
        ),
        "feedback_delay_hours": settings.feedback_delay_hours,
        "max_push_per_week": settings.max_push_per_week,
        "quiet_hours_start": settings.quiet_hours_start,
        "quiet_hours_end": settings.quiet_hours_end,
    }


@router.get("/settings/", response=ClubSettingsOut)
def get_club_settings(request):
    settings = get_or_create_club_settings(request.club.id)
    return _club_settings_payload(request, settings)


@router.put("/settings/", response=ClubSettingsOut)
@role_required("owner", "admin")
def update_club_settings(request, payload: ClubSettingsIn):
    fields = {}
    for field_name in (
        "freeze_enabled",
        "freeze_max_days",
        "freeze_max_count",
        "primary_color",
        "accent_color",
        "club_name_display",
        "logo_url",
    ):
        if field_name in payload.model_fields_set:
            fields[field_name] = getattr(payload, field_name)
    settings = update_club_settings_service(club_id=request.club.id, **fields)
    return _club_settings_payload(request, settings)


# --- Expenses ---


@router.get("/expenses/", response=list[ExpenseOut])
@role_required("owner", "admin")
@paginate(LimitOffsetPagination)
def list_expenses(request):
    return get_expenses(club=request.club)


@router.post("/expenses/", response={201: ExpenseOut})
@role_required("owner", "admin")
def create_expense_endpoint(request, payload: ExpenseIn):
    expense = create_expense(
        club_id=request.club.id,
        name=payload.name,
        amount=payload.amount,
        date=payload.date,
        category=payload.category,
        is_recurring=payload.is_recurring,
    )
    return 201, expense


@router.patch("/expenses/{expense_id}/", response=ExpenseOut)
@role_required("owner", "admin")
def update_expense_endpoint(request, expense_id: int, payload: ExpenseUpdate):
    fields = {}
    for field_name in ("name", "amount", "date", "category", "is_recurring"):
        if field_name in payload.model_fields_set:
            fields[field_name] = getattr(payload, field_name)
    return update_expense(
        expense_id=expense_id,
        club_id=request.club.id,
        **fields,
    )


@router.delete("/expenses/{expense_id}/", response={204: None})
@role_required("owner", "admin")
def delete_expense_endpoint(request, expense_id: int):
    delete_expense(expense_id=expense_id, club_id=request.club.id)
    return 204, None
