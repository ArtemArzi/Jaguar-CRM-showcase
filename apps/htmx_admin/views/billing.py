import logging
from datetime import date, timedelta
from urllib.parse import urlencode, urlsplit

from django.conf import settings
from django.core.paginator import Paginator
from django.db import models
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils import timezone

from apps.attendance.selectors import get_schedules
from apps.billing.models import (
    BankPaymentOrder,
    BankPaymentOrderReviewEvent,
    Debt,
    Payment,
    PaymentRefund,
    Subscription,
    SubscriptionFreeze,
    TrainingType,
)
from apps.billing.selectors import (
    apply_debtor_quick_filter,
    current_active_subscription_q,
    export_debtors_excel,
    get_bank_payment_order_by_id,
    get_club_subscriptions,
    get_debtors,
    get_live_online_payment_workspace_orders,
    get_manual_payment_review_queue,
    get_online_payment_review_queue,
    get_online_refund_action_queue,
    get_online_refund_payroll_action_queue,
    get_payment_history,
    get_pending_subscription_freezes,
    get_tariffs,
)
from apps.billing.services import approve_freeze as billing_approve_freeze
from apps.billing.services import create_bank_payment_order as billing_create_bank_payment_order
from apps.billing.services import create_subscription as billing_create_subscription
from apps.billing.services import freeze_subscription as billing_freeze_subscription
from apps.billing.services import reject_freeze as billing_reject_freeze
from apps.billing.services import resolve_bank_payment_order_manual_review
from apps.billing.services import unfreeze_subscription as billing_unfreeze_subscription
from apps.billing.services import verify_payment as billing_verify_payment
from apps.billing.services import write_off_debt as billing_write_off_debt
from apps.clubs.models import Location
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required
from apps.students.models import Student
from apps.students.selectors import get_students
from apps.trainers.selectors import get_trainers

logger = logging.getLogger(__name__)

SUBSCRIPTION_STATUS_COLORS = {
    "active": "bg-green-100 text-green-800",
    "pending": "bg-yellow-100 text-yellow-800",
    "frozen": "bg-blue-100 text-blue-800",
    "expired": "bg-gray-100 text-gray-600",
    "cancelled": "bg-red-100 text-red-800",
}

DEBTOR_QUICK_FILTERS = {"", "week", "large", "unverified"}
FULL_REFUND_REVIEW_CODES = {"bank_payment_refunded_requires_review"}
PARTIAL_REFUND_REVIEW_CODES = {"bank_payment_refunded_partially_requires_review"}
ONLINE_ORDER_STATUS_LABELS = {
    BankPaymentOrder.Status.CREATED: "Ссылка создаётся",
    BankPaymentOrder.Status.PENDING: "Ожидает оплаты",
    BankPaymentOrder.Status.AUTHORIZED: "Проверяется банком",
}
ONLINE_PROVIDER_LABELS = {
    BankPaymentOrder.Provider.TOCHKA: "Точка",
    BankPaymentOrder.Provider.MOCK: "Тестовый провайдер",
}
ONLINE_PROVIDER_STATUS_LABELS = {
    "approved": "Оплата подтверждена",
    "authorized": "Оплата авторизована",
    "pending": "Оплата обрабатывается",
    "rejected": "Оплата отклонена",
    "cancelled": "Оплата отменена",
    "expired": "Ссылка истекла",
    "refunded": "Оплата возвращена",
    "refunded_partially": "Частичный возврат",
}
PAYMENT_WORKSPACE_QUEUES = {"manual", "online", "history"}
PAYMENT_WORKSPACE_CONTEXTS = {"", "group", "personal", "renewal", "other"}
PAYMENT_WORKSPACE_METHODS = {"", Payment.Method.CASH, Payment.Method.TRANSFER, Payment.Method.ONLINE}
PAYMENT_HISTORY_STATUSES = {"", "all", Payment.Status.CONFIRMED, Payment.Status.REJECTED}


def _freeze_inbox_context(request: HttpRequest, *, error: str = "") -> dict:
    return {
        "pending_freezes": get_pending_subscription_freezes(club=request.club),
        "freeze_inbox_error": error,
    }


def _safe_staff_payment_url(order: BankPaymentOrder) -> str:
    try:
        parsed = urlsplit(order.provider_payment_url)
    except (TypeError, ValueError):
        return ""
    is_https = parsed.scheme == "https"
    is_local_mock = bool(
        order.provider == BankPaymentOrder.Provider.MOCK
        and settings.DEBUG
        and settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED
        and parsed.scheme == "http"
        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    )
    if not (
        (is_https or is_local_mock)
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
        and order.expires_at > timezone.now()
    ):
        return ""
    return order.provider_payment_url


def _online_creation_available() -> bool:
    if settings.PAYMENT_PROVIDER == BankPaymentOrder.Provider.MOCK:
        return bool(
            settings.DEBUG
            and settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED
            and settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED
        )
    from apps.billing.service_modules.payment_readiness import get_online_payment_capability

    return get_online_payment_capability().enabled


def _render_freeze_inbox(request: HttpRequest, *, error: str = "", status: int = 200) -> HttpResponse:
    return render(
        request,
        "dashboard/billing/_freeze_requests.html",
        _freeze_inbox_context(request, error=error),
        status=status,
    )


@management_view_required
def debtor_list(request: HttpRequest) -> HttpResponse:
    context = _debtor_list_context(request)
    return _render_debtor_list(request, context)


def _debtor_request_data(request: HttpRequest):
    return request.GET if request.method == "GET" else request.POST


def _parse_debtor_filters(request: HttpRequest) -> dict:
    data = _debtor_request_data(request)
    current_filter = data.get("filter", "")
    if current_filter not in DEBTOR_QUICK_FILTERS:
        current_filter = ""
    group_id = data.get("group_id", "")
    trainer_id = data.get("trainer_id", "")
    location_id = data.get("location_id", "")
    student_status = data.get("student_status", "")
    date_from_str = data.get("date_from", "")
    date_to_str = data.get("date_to", "")
    page_num = data.get("page", "1")
    filters: dict = {}
    if group_id:
        try:
            filters["group_id"] = int(group_id)
        except ValueError:
            pass
    if trainer_id:
        try:
            filters["trainer_id"] = int(trainer_id)
        except ValueError:
            pass
    if location_id:
        try:
            filters["location_id"] = int(location_id)
        except ValueError:
            pass
    if student_status:
        filters["student_status"] = student_status
    if date_from_str:
        try:
            filters["date_from"] = date.fromisoformat(date_from_str)
        except ValueError:
            pass
    if date_to_str:
        try:
            filters["date_to"] = date.fromisoformat(date_to_str)
        except ValueError:
            pass
    return {
        "filters": filters,
        "current_filter": current_filter,
        "group_id": group_id,
        "trainer_id": trainer_id,
        "location_id": location_id,
        "student_status": student_status,
        "date_from": date_from_str,
        "date_to": date_to_str,
        "page_num": page_num,
    }


def _debtor_export_url(state: dict) -> str:
    pairs = [
        ("filter", state["current_filter"]),
        ("group_id", state["group_id"]),
        ("trainer_id", state["trainer_id"]),
        ("location_id", state["location_id"]),
        ("student_status", state["student_status"]),
        ("date_from", state["date_from"]),
        ("date_to", state["date_to"]),
    ]
    query = urlencode([(key, value) for key, value in pairs if value])
    return f"/dashboard/billing/debtors/export/?{query}" if query else "/dashboard/billing/debtors/export/"


def _debtor_list_context(request: HttpRequest, *, error: str = "") -> dict:
    state = _parse_debtor_filters(request)
    debtors = apply_debtor_quick_filter(
        get_debtors(club=request.club, **state["filters"]),
        quick_filter=state["current_filter"],
    )
    total_debt = debtors.aggregate(total=models.Sum("tariff_price"))["total"] or 0

    paginator = Paginator(debtors, 20)
    page_obj = paginator.get_page(state["page_num"])

    schedules = get_schedules(club=request.club)
    trainers = get_trainers(club=request.club)
    locations = Location.objects.filter(club=request.club)

    context = {
        "page_title": "Должники",
        "active_tab": "debtors",
        "total_debt": total_debt,
        "page_obj": page_obj,
        "debtor_error": error,
        "debtor_export_url": _debtor_export_url(state),
        "schedules": schedules,
        "trainers": trainers,
        "locations": locations,
        "statuses": Student.Status.choices,
        "current_filter": state["current_filter"],
        "current_group_id": state["group_id"],
        "current_trainer_id": state["trainer_id"],
        "current_location_id": state["location_id"],
        "current_student_status": state["student_status"],
        "current_date_from": state["date_from"],
        "current_date_to": state["date_to"],
    }
    return context


def _render_debtor_list(request: HttpRequest, context: dict, *, status: int = 200) -> HttpResponse:
    template = "dashboard/billing/debtors.html#content" if request.htmx else "dashboard/billing/debtors.html"
    return render(request, template, context, status=status)


def _debt_writeoff_error_message(exc: BusinessLogicError) -> str:
    if exc.code == "writeoff_reason_required":
        return "Укажите комментарий к списанию долга."
    if exc.code == "debt_payment_pending":
        return "Долг привязан к ожидающей оплате. Сначала подтвердите или отклоните оплату."
    if exc.code == "debt_already_resolved":
        return "Долг уже закрыт."
    if exc.code == "payroll_period_closed":
        return "Период выплат за дату долга уже закрыт. Списание заблокировано."
    return "Не удалось списать долг. Проверьте статус и попробуйте ещё раз."


@management_view_required
def debtor_export(request: HttpRequest) -> HttpResponse:
    state = _parse_debtor_filters(request)
    data = export_debtors_excel(
        club=request.club,
        **state["filters"],
        quick_filter=state["current_filter"],
    )
    response = HttpResponse(
        data,
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = 'attachment; filename="debtors.xlsx"'
    return response


@management_view_required
def debtor_write_off_action(request: HttpRequest, debt_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponse(status=405)

    try:
        billing_write_off_debt(
            debt_id=debt_id,
            club_id=request.club.id,
            written_off_by_id=request.user.id,
            reason=request.POST.get("reason", ""),
        )
    except Debt.DoesNotExist:
        raise Http404
    except BusinessLogicError as exc:
        context = _debtor_list_context(request, error=_debt_writeoff_error_message(exc))
        return _render_debtor_list(request, context)

    logger.info(
        "debt_written_off_via_admin",
        extra={"debt_id": debt_id, "club_id": request.club.id},
    )
    return _render_debtor_list(request, _debtor_list_context(request))


@management_view_required
def _parse_payment_workspace_state(
    request: HttpRequest,
    *,
    queue_override: str | None = None,
) -> dict:
    requested_queue = request.GET.get("queue", "")
    queue = queue_override or requested_queue
    if not queue:
        legacy_status = request.GET.get("status", "")
        if legacy_status and legacy_status in PAYMENT_HISTORY_STATUSES:
            queue = "history"
        elif request.GET.get("bank_order", "") or request.GET.get("review_order", ""):
            queue = "online"
        else:
            queue = "manual"
    if queue not in PAYMENT_WORKSPACE_QUEUES:
        queue = "manual"
    payment_method = request.GET.get("method", "")
    if payment_method not in PAYMENT_WORKSPACE_METHODS:
        payment_method = ""
    if queue == "online":
        payment_method = Payment.Method.ONLINE
    elif queue == "manual" and payment_method == Payment.Method.ONLINE:
        payment_method = ""
    context = request.GET.get("context", "")
    if context not in PAYMENT_WORKSPACE_CONTEXTS:
        context = ""
    status = request.GET.get("status", "")
    if status not in PAYMENT_HISTORY_STATUSES:
        status = ""

    def optional_positive_int(name: str) -> int | None:
        try:
            value = int(request.GET.get(name, ""))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    def optional_date(name: str) -> date | None:
        try:
            return date.fromisoformat(request.GET.get(name, ""))
        except (TypeError, ValueError):
            return None

    return {
        "queue": queue,
        "page": request.GET.get("page", "1"),
        "refund_page": request.GET.get("refund_page", "1"),
        "payroll_page": request.GET.get("payroll_page", "1"),
        "trainer_id": optional_positive_int("trainer_id"),
        "payment_method": payment_method,
        "context": context,
        "date_from": optional_date("date_from"),
        "date_to": optional_date("date_to"),
        "status": status,
        "payment_id": optional_positive_int("payment_id"),
        "bank_order_id": optional_positive_int("bank_order"),
        "review_order_id": optional_positive_int("review_order"),
    }


def _payment_workspace_url(state: dict, **overrides) -> str:
    values = {
        "queue": state["queue"],
        "trainer_id": state["trainer_id"] or "",
        "method": state["payment_method"],
        "context": state["context"],
        "date_from": state["date_from"].isoformat() if state["date_from"] else "",
        "date_to": state["date_to"].isoformat() if state["date_to"] else "",
        "status": state["status"],
        "payment_id": state["payment_id"] or "",
        "bank_order": state["bank_order_id"] or "",
        "review_order": state["review_order_id"] or "",
        "refund_page": state["refund_page"] if state["refund_page"] != "1" else "",
        "payroll_page": state["payroll_page"] if state["payroll_page"] != "1" else "",
    }
    values.update(overrides)
    query = urlencode([(key, value) for key, value in values.items() if value not in (None, "")])
    return f"/dashboard/billing/payments/?{query}" if query else "/dashboard/billing/payments/"


def _annotate_payment_workspace_context(payment: Payment, *, queue: str) -> None:
    personal_link = getattr(payment, "personal_drop_in_payment_link", None)
    personal_booking = getattr(personal_link, "booking", None)
    payment.workspace_personal_booking = personal_booking
    payment.workspace_personal_terms = (
        getattr(personal_booking, "terms_snapshot", None) if personal_booking else None
    )
    if payment.subscription_id and payment.subscription.renewed_from_id:
        payment.workspace_context_label = "Продление абонемента"
        payment.workspace_service_label = (
            payment.renewal_source_tariff_name_snapshot or payment.tariff.name
        )
    elif payment.target_training_group_id or (
        payment.target_schedule_id and payment.tariff.training_type.kind == TrainingType.Kind.GROUP
    ):
        payment.workspace_context_label = "Групповое обучение"
        payment.workspace_service_label = payment.target_group_name_snapshot or payment.tariff.name
    elif personal_booking is not None:
        payment.workspace_context_label = "Персональная тренировка"
        payment.workspace_service_label = (
            getattr(payment.workspace_personal_terms, "tariff_name_snapshot", "")
            or personal_booking.tariff_name_snapshot
        )
    elif payment.tariff.training_type.kind == TrainingType.Kind.PERSONAL:
        payment.workspace_context_label = "Персональная тренировка"
        payment.workspace_service_label = payment.tariff.name
    else:
        payment.workspace_context_label = "Абонемент"
        payment.workspace_service_label = payment.tariff.name
    payment.workspace_student_path = f"/dashboard/students/{payment.student_id}/card/"
    if queue == "history":
        payment.workspace_resource_path = ""
        payment.workspace_resource_label = ""
    elif payment.status in {Payment.Status.CONFIRMED, Payment.Status.REJECTED}:
        payment.workspace_resource_path = (
            f"/dashboard/billing/payments/?queue=history&payment_id={payment.id}#payment-{payment.id}"
        )
        payment.workspace_resource_label = "История оплаты"
    else:
        payment.workspace_resource_path = (
            f"/dashboard/billing/payments/?queue=manual&payment_id={payment.id}#payment-{payment.id}"
        )
        payment.workspace_resource_label = "Открыть операцию"
    payment.workspace_schedule_path = ""
    if payment.target_schedule_id and payment.target_start_date:
        payment.workspace_schedule_path = (
            f"/dashboard/schedule/{payment.target_schedule_id}/detail/"
            f"?date={payment.target_start_date.isoformat()}"
        )
    elif personal_booking is not None:
        enrollment = personal_booking.enrollment
        payment.workspace_schedule_path = (
            f"/dashboard/schedule/{enrollment.schedule_id}/detail/"
            f"?date={enrollment.starts_on.isoformat()}"
        )


def _short_provider_reference(value: str) -> str:
    if not value:
        return ""
    return value if len(value) <= 12 else f"…{value[-12:]}"


def _annotate_online_review_order(order: BankPaymentOrder) -> None:
    _annotate_payment_workspace_context(order.payment, queue="online")
    order.workspace_context_label = order.payment.workspace_context_label
    order.workspace_service_label = order.payment.workspace_service_label
    personal_reservation = getattr(order, "personal_payment_reservation", None)
    if personal_reservation is not None:
        reservation_terms = getattr(personal_reservation, "terms_snapshot", None)
        order.workspace_service_label = (
            getattr(reservation_terms, "tariff_name_snapshot", "")
            or order.purpose_snapshot
        )
    order.workspace_personal_booking = order.payment.workspace_personal_booking
    order.workspace_student_path = order.payment.workspace_student_path
    order.workspace_resource_path = (
        f"/dashboard/billing/payments/?queue=online&review_order={order.id}#bank-order-{order.id}"
    )
    order.workspace_resource_label = "Открыть операцию"
    order.workspace_schedule_path = order.payment.workspace_schedule_path
    provider_events = getattr(order, "workspace_provider_events", [])
    provider_event = provider_events[0] if provider_events else None
    order.workspace_provider_evidence_available = provider_event is not None
    order.workspace_provider_name = ONLINE_PROVIDER_LABELS.get(
        order.provider,
        order.get_provider_display(),
    )
    order.workspace_provider_status_label = ""
    order.workspace_provider_reference = ""
    order.workspace_provider_evidence_at = None
    if provider_event is not None:
        normalized_status = provider_event.normalized_status_snapshot
        order.workspace_provider_status_label = ONLINE_PROVIDER_STATUS_LABELS.get(
            normalized_status,
            normalized_status,
        )
        order.workspace_provider_reference = _short_provider_reference(
            provider_event.provider_operation_id
            or provider_event.provider_payment_link_id
        )
        order.workspace_provider_evidence_at = (
            provider_event.provider_paid_at_snapshot
            or provider_event.processed_at
            or provider_event.received_at
        )
    order.is_full_refund_review = order.last_error_code in FULL_REFUND_REVIEW_CODES
    order.is_partial_refund_review = order.last_error_code in PARTIAL_REFUND_REVIEW_CODES
    order.is_refund_review = order.is_full_refund_review or order.is_partial_refund_review
    if order.is_full_refund_review:
        order.admin_review_summary = (
            "Банк сообщил о полном возврате. Зафиксируйте решение по правам посещений."
        )
    elif order.is_partial_refund_review:
        order.admin_review_summary = (
            "Банк сообщил о частичном возврате. Зафиксируйте сумму возврата."
        )
    elif order.provider == BankPaymentOrder.Provider.TOCHKA:
        order.admin_review_summary = (
            "Автоматическая проверка не завершена. Запросите безопасную сверку с банком."
        )
    else:
        order.admin_review_summary = "Оплата требует проверки в тестовом контуре."


@management_view_required
def _payment_list_context(
    request: HttpRequest,
    *,
    error: str = "",
    queue_override: str | None = None,
) -> dict:
    state = _parse_payment_workspace_state(request, queue_override=queue_override)
    filter_kwargs = {
        "trainer_id": state["trainer_id"],
        "payment_method": state["payment_method"],
        "context": state["context"],
        "date_from": state["date_from"],
        "date_to": state["date_to"],
    }
    manual_queue = get_manual_payment_review_queue(club=request.club, **filter_kwargs)
    online_queue = get_online_payment_review_queue(
        club=request.club,
        trainer_id=state["trainer_id"],
        context=state["context"],
        date_from=state["date_from"],
        date_to=state["date_to"],
    )
    if state["queue"] == "manual":
        rows = manual_queue
        if state["payment_id"] is not None:
            rows = rows.filter(id=state["payment_id"])
    elif state["queue"] == "online":
        rows = online_queue
        if state["review_order_id"] is not None:
            rows = rows.filter(id=state["review_order_id"])
    else:
        rows = get_payment_history(
            club=request.club,
            status=state["status"],
            **filter_kwargs,
        )
        if state["payment_id"] is not None:
            rows = rows.filter(id=state["payment_id"])

    paginator = Paginator(rows, 20)
    page_obj = paginator.get_page(state["page"])
    manual_review_orders: list[BankPaymentOrder] = []
    if state["queue"] == "online":
        manual_review_orders = list(page_obj.object_list)
        for order in manual_review_orders:
            _annotate_online_review_order(order)
    else:
        for payment in page_obj.object_list:
            _annotate_payment_workspace_context(payment, queue=state["queue"])

    live_bank_payment_orders = []
    if state["queue"] == "online" and state["bank_order_id"] is not None:
        live_bank_payment_orders = list(
            get_live_online_payment_workspace_orders(
                club=request.club,
                trainer_id=state["trainer_id"],
                context=state["context"],
                date_from=state["date_from"],
                date_to=state["date_to"],
            ).filter(id=state["bank_order_id"])
        )
    for order in live_bank_payment_orders:
        order.safe_staff_payment_url = _safe_staff_payment_url(order)
        order.admin_status_label = ONLINE_ORDER_STATUS_LABELS.get(
            order.status,
            "Проверяется",
        )
    standalone_refund_cases = []
    payroll_action_refunds = []
    refund_previous_url = ""
    refund_next_url = ""
    payroll_previous_url = ""
    payroll_next_url = ""
    if state["queue"] == "online":
        refund_page_obj = Paginator(
            get_online_refund_action_queue(
                club=request.club,
                trainer_id=state["trainer_id"],
                context=state["context"],
                date_from=state["date_from"],
                date_to=state["date_to"],
            ),
            20,
        ).get_page(state["refund_page"])
        standalone_refund_cases = list(refund_page_obj.object_list)
        refund_previous_url = (
            _payment_workspace_url(
                state,
                refund_page=refund_page_obj.previous_page_number(),
            )
            if refund_page_obj.has_previous()
            else ""
        )
        refund_next_url = (
            _payment_workspace_url(
                state,
                refund_page=refund_page_obj.next_page_number(),
            )
            if refund_page_obj.has_next()
            else ""
        )
        payroll_page_obj = Paginator(
            get_online_refund_payroll_action_queue(
                club=request.club,
                trainer_id=state["trainer_id"],
                context=state["context"],
                date_from=state["date_from"],
                date_to=state["date_to"],
            ),
            20,
        ).get_page(state["payroll_page"])
        payroll_action_refunds = list(payroll_page_obj.object_list)
        payroll_previous_url = (
            _payment_workspace_url(
                state,
                payroll_page=payroll_page_obj.previous_page_number(),
            )
            if payroll_page_obj.has_previous()
            else ""
        )
        payroll_next_url = (
            _payment_workspace_url(
                state,
                payroll_page=payroll_page_obj.next_page_number(),
            )
            if payroll_page_obj.has_next()
            else ""
        )

    trainers = get_trainers(club=request.club).order_by("last_name", "first_name", "id")
    previous_url = (
        _payment_workspace_url(state, page=page_obj.previous_page_number())
        if page_obj.has_previous()
        else ""
    )
    next_url = (
        _payment_workspace_url(state, page=page_obj.next_page_number())
        if page_obj.has_next()
        else ""
    )

    context = {
        "page_title": "Финансовые операции",
        "active_tab": "payments",
        "page_obj": page_obj,
        "current_queue": state["queue"],
        "current_status": state["status"],
        "current_method": state["payment_method"],
        "current_context": state["context"],
        "current_trainer_id": str(state["trainer_id"] or ""),
        "current_date_from": state["date_from"].isoformat() if state["date_from"] else "",
        "current_date_to": state["date_to"].isoformat() if state["date_to"] else "",
        "trainers": trainers,
        "manual_queue_url": _payment_workspace_url(
            state, queue="manual", page="", status="", method="", payment_id="", bank_order="",
            review_order="",
            refund_page="", payroll_page="",
        ),
        "online_queue_url": _payment_workspace_url(
            state, queue="online", page="", status="", method="", payment_id="", bank_order="",
            review_order="",
            refund_page="", payroll_page="",
        ),
        "history_queue_url": _payment_workspace_url(
            state, queue="history", page="", payment_id="", bank_order="", review_order="",
            refund_page="", payroll_page="",
        ),
        "history_all_url": _payment_workspace_url(
            state, queue="history", page="", status="", payment_id="", bank_order="",
            review_order="",
            refund_page="", payroll_page="",
        ),
        "history_confirmed_url": _payment_workspace_url(
            state,
            queue="history",
            page="",
            status=Payment.Status.CONFIRMED,
            payment_id="",
            bank_order="",
            review_order="",
        ),
        "history_rejected_url": _payment_workspace_url(
            state,
            queue="history",
            page="",
            status=Payment.Status.REJECTED,
            payment_id="",
            bank_order="",
            review_order="",
        ),
        "previous_url": previous_url,
        "next_url": next_url,
        "refund_previous_url": refund_previous_url,
        "refund_next_url": refund_next_url,
        "payroll_previous_url": payroll_previous_url,
        "payroll_next_url": payroll_next_url,
        "pending_count": get_manual_payment_review_queue(club=request.club).count(),
        "live_bank_payment_orders": live_bank_payment_orders,
        "live_bank_payment_order_count": len(live_bank_payment_orders),
        "manual_review_orders": manual_review_orders,
        "manual_review_count": (
            page_obj.paginator.count if state["queue"] == "online" else 0
        ),
        "online_action_count": (
            get_online_payment_review_queue(club=request.club).count()
            + get_online_refund_action_queue(club=request.club).count()
            + get_online_refund_payroll_action_queue(club=request.club).count()
        ),
        "standalone_refund_cases": standalone_refund_cases,
        "standalone_refund_case_count": len(standalone_refund_cases),
        "payroll_action_refunds": payroll_action_refunds,
        "payroll_action_refund_count": len(payroll_action_refunds),
        "payment_error": error,
    }
    return context


def _render_payment_list(request: HttpRequest, context: dict, *, status: int = 200) -> HttpResponse:
    if request.htmx:
        return render(request, "dashboard/billing/payments.html#content", context, status=status)
    return render(request, "dashboard/billing/payments.html", context, status=status)


@management_view_required
def payment_list(request: HttpRequest) -> HttpResponse:
    return _render_payment_list(request, _payment_list_context(request))


@management_view_required
def verify_payment_action(request: HttpRequest, payment_id: int) -> HttpResponse:
    action = request.POST.get("action", "")
    if action not in ("confirm", "reject"):
        return HttpResponse(status=400)
    rejection_reason = request.POST.get("rejection_reason", "").strip() if action == "reject" else ""
    if action == "reject" and not rejection_reason:
        return _render_payment_list(
            request,
            _payment_list_context(
                request,
                error="Укажите причину отклонения оплаты",
                queue_override="manual",
            ),
        )

    try:
        billing_verify_payment(
            payment_id=payment_id,
            club_id=request.club.id,
            verified_by_id=request.user.id,
            action=action,
            rejection_reason=rejection_reason,
        )
    except Payment.DoesNotExist:
        raise Http404
    except BusinessLogicError as exc:
        return _render_payment_list(
            request,
            _payment_list_context(request, error=exc.message, queue_override="manual"),
        )

    logger.info(
        "payment_verified_via_admin",
        extra={"payment_id": payment_id, "club_id": request.club.id, "action": action},
    )
    return HttpResponse(
        status=204,
        headers={"HX-Redirect": "/dashboard/billing/payments/"},
    )


@management_view_required
def bank_payment_order_review_action(request: HttpRequest, order_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponse(status=405)

    resolution = request.POST.get("resolution", "")
    reason = request.POST.get("reason", "").strip()
    if resolution not in {
        BankPaymentOrderReviewEvent.Resolution.CONFIRM_PAID,
        BankPaymentOrderReviewEvent.Resolution.REJECT,
        BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED,
        BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY,
    }:
        return HttpResponse(status=400)
    if resolution in {
        BankPaymentOrderReviewEvent.Resolution.CONFIRM_PAID,
        BankPaymentOrderReviewEvent.Resolution.REJECT,
        BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED,
        BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY,
    } and not reason:
        return _render_payment_list(
            request,
            _payment_list_context(
                request,
                error="Укажите комментарий для решения по спорной оплате",
                queue_override="online",
            ),
            status=400,
        )
    evidence = {}
    if resolution == BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED:
        entitlement_action = request.POST.get("entitlement_action", "").strip()
        if entitlement_action not in {
            PaymentRefund.EntitlementDisposition.KEEP_CLUB_ABSORBS,
            PaymentRefund.EntitlementDisposition.REVOKE_REMAINING,
        }:
            return _render_payment_list(
                request,
                _payment_list_context(
                    request,
                    error="Выберите, сохранить или отозвать оставшиеся права посещения",
                    queue_override="online",
                ),
                status=400,
            )
        evidence["entitlement_action"] = entitlement_action
        legacy_enrollment_action = request.POST.get("legacy_enrollment_action", "").strip()
        legacy_enrollment_id = request.POST.get("legacy_enrollment_id", "").strip()
        if legacy_enrollment_action:
            evidence["legacy_enrollment_action"] = legacy_enrollment_action
        if legacy_enrollment_id:
            evidence["legacy_enrollment_id"] = legacy_enrollment_id
    if resolution == BankPaymentOrderReviewEvent.Resolution.MARK_REFUNDED_PARTIALLY:
        refund_amount = request.POST.get("refund_amount", "").strip()
        if not refund_amount:
            return _render_payment_list(
                request,
                _payment_list_context(
                    request,
                    error="Укажите сумму частичного возврата",
                    queue_override="online",
                ),
                status=400,
            )
        evidence["refund_amount"] = refund_amount

    try:
        resolve_bank_payment_order_manual_review(
            club_id=request.club.id,
            order_id=order_id,
            actor_user_id=request.user.id,
            resolution=resolution,
            reason=reason,
            evidence=evidence,
        )
    except BankPaymentOrder.DoesNotExist:
        raise Http404
    except BusinessLogicError as exc:
        return _render_payment_list(
            request,
            _payment_list_context(request, error=exc.message, queue_override="online"),
            status=400,
        )

    logger.info(
        "bank_payment_order_review_resolved_via_admin",
        extra={"order_id": order_id, "club_id": request.club.id, "resolution": resolution},
    )
    return HttpResponse(
        status=204,
        headers={"HX-Redirect": "/dashboard/billing/payments/?queue=online"},
    )


@management_view_required
def bank_payment_order_reconcile_action(request: HttpRequest, order_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponse(status=405)

    try:
        order = get_bank_payment_order_by_id(club=request.club, order_id=order_id)
        if (
            order.provider != BankPaymentOrder.Provider.TOCHKA
            or order.status != BankPaymentOrder.Status.MANUAL_REVIEW
            or order.last_error_code in FULL_REFUND_REVIEW_CODES | PARTIAL_REFUND_REVIEW_CODES
        ):
            raise BusinessLogicError(
                "Сверка с банком недоступна для этой оплаты",
                code="bank_payment_reconciliation_not_available",
            )
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
    except BankPaymentOrder.DoesNotExist:
        raise Http404
    except BusinessLogicError as exc:
        return _render_payment_list(
            request,
            _payment_list_context(request, error=exc.message, queue_override="online"),
            status=400,
        )

    logger.info(
        "bank_payment_order_reconciliation_requested_via_admin",
        extra={"order_id": order_id, "club_id": request.club.id},
    )
    return HttpResponse(
        status=204,
        headers={"HX-Redirect": "/dashboard/billing/payments/?queue=online"},
    )


@management_view_required
def payment_refund_case_approve_action(request: HttpRequest, case_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponse(status=405)
    reason = request.POST.get("reason", "").strip()
    amount = request.POST.get("amount", "").strip()
    refund_kind = request.POST.get("refund_kind", "").strip()
    entitlement_action = request.POST.get("entitlement_action", "").strip() or None
    legacy_enrollment_action = request.POST.get("legacy_enrollment_action", "").strip() or None
    legacy_enrollment_id_raw = request.POST.get("legacy_enrollment_id", "").strip()
    try:
        legacy_enrollment_id = int(legacy_enrollment_id_raw) if legacy_enrollment_id_raw else None
    except ValueError:
        return _render_payment_list(
            request,
            _payment_list_context(
                request,
                error="Некорректное зачисление для возврата",
                queue_override="online",
            ),
            status=400,
        )

    from apps.billing.refund_services import approve_payment_refund_case

    try:
        approve_payment_refund_case(
            club_id=request.club.id,
            case_id=case_id,
            actor_user_id=request.user.id,
            idempotency_key=f"payment-refund-case-{case_id}",
            amount=amount,
            refund_kind=refund_kind,
            reason=reason,
            entitlement_action=entitlement_action,
            legacy_enrollment_action=legacy_enrollment_action,
            legacy_enrollment_id=legacy_enrollment_id,
        )
    except BusinessLogicError as exc:
        return _render_payment_list(
            request,
            _payment_list_context(request, error=exc.message, queue_override="online"),
            status=400,
        )
    return HttpResponse(
        status=204,
        headers={"HX-Redirect": "/dashboard/billing/payments/?queue=online"},
    )


@management_view_required
def payment_refund_payroll_action(request: HttpRequest, refund_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponse(status=405)
    try:
        effective_date = date.fromisoformat(request.POST.get("effective_date", ""))
    except ValueError:
        return _render_payment_list(
            request,
            _payment_list_context(
                request,
                error="Выберите дату зарплатной корректировки",
                queue_override="online",
            ),
            status=400,
        )

    from apps.billing.refund_services import complete_payment_refund_payroll

    try:
        complete_payment_refund_payroll(
            club_id=request.club.id,
            refund_id=refund_id,
            actor_user_id=request.user.id,
            effective_date=effective_date,
        )
    except PaymentRefund.DoesNotExist:
        raise Http404
    except BusinessLogicError as exc:
        return _render_payment_list(
            request,
            _payment_list_context(request, error=exc.message, queue_override="online"),
            status=400,
        )
    return HttpResponse(
        status=204,
        headers={"HX-Redirect": "/dashboard/billing/payments/?queue=online"},
    )


# --- Subscriptions ---


@management_view_required
def subscription_list(request: HttpRequest) -> HttpResponse:
    student_id = request.GET.get("student_id", "")
    status_filter = request.GET.get("status", "")
    page_num = request.GET.get("page", "1")

    subs = get_club_subscriptions(
        club=request.club,
        student_id=int(student_id) if student_id else None,
    ).select_related("student").order_by("-activated_at")

    if status_filter == "active":
        subs = subs.filter(current_active_subscription_q())
    elif status_filter == "expiring":
        now = timezone.now()
        subs = subs.filter(
            current_active_subscription_q(now),
            expires_at__lte=now + timedelta(days=7),
        )
    elif status_filter == "expired":
        subs = subs.filter(status=Subscription.Status.EXPIRED)
    elif status_filter == "cancelled":
        subs = subs.filter(status=Subscription.Status.CANCELLED)

    paginator = Paginator(subs, 20)
    page_obj = paginator.get_page(page_num)

    tariffs = list(get_tariffs(club=request.club))
    for tariff in tariffs:
        component_kinds = {
            component.training_type.kind
            for component in getattr(tariff, "active_components", [])
        }
        tariff.requires_package_owner = any(
            kind in {TrainingType.Kind.PERSONAL, TrainingType.Kind.MINI_GROUP}
            for kind in component_kinds
        ) or tariff.training_type.kind in {TrainingType.Kind.PERSONAL, TrainingType.Kind.MINI_GROUP}
        if TrainingType.Kind.PERSONAL in component_kinds or tariff.training_type.kind == TrainingType.Kind.PERSONAL:
            tariff.package_owner_kind = TrainingType.Kind.PERSONAL
        elif (
            TrainingType.Kind.MINI_GROUP in component_kinds
            or tariff.training_type.kind == TrainingType.Kind.MINI_GROUP
        ):
            tariff.package_owner_kind = TrainingType.Kind.MINI_GROUP
        else:
            tariff.package_owner_kind = ""
    students = get_students(club=request.club)
    from apps.trainers.selectors import get_trainers
    active_trainers = get_trainers(club=request.club)

    context = {
        "page_title": "Абонементы",
        "active_tab": "subscriptions",
        "page_obj": page_obj,
        "tariffs": tariffs,
        "students": students,
        "active_trainers": active_trainers,
        **_freeze_inbox_context(request),
        "current_student_id": student_id,
        "show_create_form": request.GET.get("new") == "1",
        "current_status": status_filter,
        "badge_colors": SUBSCRIPTION_STATUS_COLORS,
        "online_payment_creation_available": _online_creation_available(),
    }
    if request.htmx:
        return render(request, "dashboard/billing/subscriptions.html#content", context)
    return render(request, "dashboard/billing/subscriptions.html", context)


@management_view_required
def create_subscription_view(request: HttpRequest) -> HttpResponse:
    student_id = request.POST.get("student_id", "")
    tariff_id = request.POST.get("tariff_id", "")
    payment_method = request.POST.get("payment_method", "").strip()
    seller_trainer_id_raw = request.POST.get("seller_trainer_id", "").strip()
    package_owner_trainer_id_raw = request.POST.get("package_owner_trainer_id", "").strip()

    if not student_id or not tariff_id:
        return HttpResponse(status=400)
    if payment_method not in {
        Payment.Method.CASH,
        Payment.Method.TRANSFER,
        Payment.Method.ONLINE,
    }:
        return HttpResponse(status=400)

    seller_trainer_id: int | None = None
    if seller_trainer_id_raw:
        try:
            seller_trainer_id = int(seller_trainer_id_raw)
        except ValueError:
            return HttpResponse(status=400)

    package_owner_trainer_id: int | None = None
    if package_owner_trainer_id_raw:
        try:
            package_owner_trainer_id = int(package_owner_trainer_id_raw)
        except ValueError:
            return HttpResponse(status=400)

    try:
        if payment_method == Payment.Method.ONLINE:
            source = (
                BankPaymentOrder.Source.ADMIN
                if request._membership.role == "admin"
                else BankPaymentOrder.Source.OWNER
            )
            order = billing_create_bank_payment_order(
                club_id=request.club.id,
                student_id=int(student_id),
                tariff_id=int(tariff_id),
                source=source,
                created_by_id=request.user.id,
                seller_trainer_id=seller_trainer_id,
                package_owner_trainer_id=package_owner_trainer_id,
            )
        else:
            billing_create_subscription(
                club_id=request.club.id,
                student_id=int(student_id),
                tariff_id=int(tariff_id),
                seller_trainer_id=seller_trainer_id,
                package_owner_trainer_id=package_owner_trainer_id,
                recorded_by_id=request.user.id,
                payment_method=payment_method,
            )
    except BusinessLogicError as e:
        logger.warning(
            "subscription_create_failed",
            extra={"club_id": request.club.id, "error_code": e.code},
        )
        return HttpResponse(status=400)

    if payment_method == Payment.Method.ONLINE:
        logger.info(
            "bank_payment_order_created_via_admin",
            extra={"club_id": request.club.id, "order_id": order.id},
        )
        return HttpResponse(
            status=204,
            headers={"HX-Redirect": f"/dashboard/billing/payments/?bank_order={order.id}"},
        )

    logger.info(
        "subscription_created_via_admin",
        extra={"club_id": request.club.id, "student_id": student_id, "tariff_id": tariff_id},
    )
    return HttpResponse(status=204, headers={"HX-Redirect": "/dashboard/billing/subscriptions/"})


def _student_freeze_panel(request, subscription_id):
    from apps.billing.models import SubscriptionFreeze
    from apps.billing.selectors import get_subscription_by_id
    from apps.htmx_admin.views.student_operations import _card

    try:
        subscription = get_subscription_by_id(club=request.club, subscription_id=subscription_id)
    except Subscription.DoesNotExist:
        raise Http404
    context = {
        "subscription": subscription, "student": subscription.student,
        "days": request.POST.get("days", "7"), "reason": request.POST.get("reason", "vacation"),
    }
    if request.method == "POST":
        try:
            if request.POST.get("action") == "thaw":
                freeze = SubscriptionFreeze.objects.for_club(request.club).filter(
                    subscription=subscription, ends_at__isnull=True,
                    status=SubscriptionFreeze.FreezeStatus.APPROVED,
                ).first()
                if freeze is None:
                    raise BusinessLogicError("Действующая заморозка не найдена.")
                billing_unfreeze_subscription(freeze_id=freeze.id, club_id=request.club.id)
                return _card(request, subscription.student, "Абонемент разморожен.")
            days = int(context["days"])
            if days < 1 or context["reason"] not in {"vacation", "injury", "illness", "other"}:
                raise ValueError
            billing_freeze_subscription(
                club_id=request.club.id, subscription_id=subscription.id, days=days,
                reason=context["reason"], frozen_by_id=request.user.id,
            )
            return _card(request, subscription.student, "Абонемент заморожен.")
        except (ValueError, TypeError):
            context["error"] = "Укажите число дней и причину заморозки."
        except BusinessLogicError as error:
            context["error"] = str(error)
    return render(request, "dashboard/students/_freeze.html", context)


@management_view_required
def freeze_subscription_view(request: HttpRequest, subscription_id: int) -> HttpResponse:
    if request.GET.get("student_context") == "1" or request.POST.get("student_context") == "1":
        return _student_freeze_panel(request, subscription_id)
    if request.method == "GET":
        try:
            from apps.billing.selectors import get_subscription_by_id

            subscription = get_subscription_by_id(club=request.club, subscription_id=subscription_id)
        except Subscription.DoesNotExist:
            raise Http404
        context = {"subscription": subscription}
        return render(request, "dashboard/billing/_freeze_form.html", context)

    # POST
    days = request.POST.get("days", "")
    reason = request.POST.get("reason", "vacation")
    if reason not in ("vacation", "injury", "illness", "other"):
        reason = "other"

    try:
        days = int(days)
        if days < 1:
            raise ValueError
    except ValueError:
        return HttpResponse(status=400)

    try:
        billing_freeze_subscription(
            club_id=request.club.id,
            subscription_id=subscription_id,
            days=days,
            reason=reason,
            frozen_by_id=request.user.id,
        )
    except Subscription.DoesNotExist:
        raise Http404
    except BusinessLogicError as e:
        try:
            from apps.billing.selectors import get_subscription_by_id

            subscription = get_subscription_by_id(club=request.club, subscription_id=subscription_id)
        except Subscription.DoesNotExist:
            raise Http404
        context = {"subscription": subscription, "error": str(e)}
        return render(request, "dashboard/billing/_freeze_form.html", context)

    logger.info(
        "subscription_frozen_via_admin",
        extra={"club_id": request.club.id, "subscription_id": subscription_id},
    )
    return HttpResponse(status=204, headers={"HX-Redirect": "/dashboard/billing/subscriptions/"})


@management_view_required
def approve_freeze_view(request: HttpRequest, freeze_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponse(status=405)

    try:
        billing_approve_freeze(
            freeze_id=freeze_id,
            club_id=request.club.id,
            approved_by_id=request.user.id,
        )
    except SubscriptionFreeze.DoesNotExist:
        raise Http404
    except BusinessLogicError as exc:
        return _render_freeze_inbox(request, error=str(exc), status=400)

    logger.info(
        "freeze_approved_via_admin",
        extra={"club_id": request.club.id, "freeze_id": freeze_id},
    )
    return HttpResponse(status=204, headers={"HX-Redirect": "/dashboard/billing/subscriptions/"})


@management_view_required
def reject_freeze_view(request: HttpRequest, freeze_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponse(status=405)

    decision_reason = request.POST.get("decision_reason", "").strip()
    try:
        billing_reject_freeze(
            freeze_id=freeze_id,
            club_id=request.club.id,
            rejected_by_id=request.user.id,
            decision_reason=decision_reason,
        )
    except SubscriptionFreeze.DoesNotExist:
        raise Http404
    except BusinessLogicError as exc:
        return _render_freeze_inbox(request, error=str(exc), status=400)

    logger.info(
        "freeze_rejected_via_admin",
        extra={"club_id": request.club.id, "freeze_id": freeze_id},
    )
    return HttpResponse(status=204, headers={"HX-Redirect": "/dashboard/billing/subscriptions/"})


@management_view_required
def unfreeze_subscription_view(request: HttpRequest, subscription_id: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponse(status=405)

    from apps.billing.models import SubscriptionFreeze

    freeze = (
        SubscriptionFreeze.objects.for_club(request.club)
        .filter(subscription_id=subscription_id, ends_at__isnull=True)
        .first()
    )
    if not freeze:
        raise Http404

    try:
        billing_unfreeze_subscription(
            freeze_id=freeze.id,
            club_id=request.club.id,
        )
    except BusinessLogicError as e:
        logger.warning("unfreeze_failed", extra={"subscription_id": subscription_id, "error": str(e)})
        return HttpResponse(str(e), status=400)

    logger.info(
        "subscription_unfrozen_via_admin",
        extra={"club_id": request.club.id, "subscription_id": subscription_id},
    )
    return HttpResponse(status=204, headers={"HX-Redirect": "/dashboard/billing/subscriptions/"})
