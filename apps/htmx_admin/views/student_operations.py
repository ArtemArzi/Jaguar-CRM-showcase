"""Thin HTMX adapters for the student-card domain commands."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from apps.attendance.models import Checkin
from apps.attendance.services.student_corrections import (
    cancel_student_attendance,
    preview_student_attendance,
    record_student_attendance,
)
from apps.billing.models import BankPaymentOrder, Subscription, SubscriptionComponent
from apps.billing.service_modules.subscription_corrections import (
    correct_subscription,
    get_subscription_correction_state,
)
from apps.clubs.timezones import club_localdate
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required
from apps.htmx_admin.views.students import _student_card_context
from apps.students.models import Student
from apps.students.operation_selectors import (
    get_student_attendance_options,
    get_student_operation_history,
    subscription_expires_on,
)
from apps.students.selectors import get_student_detail


def _student(request, student_id):
    try:
        return get_student_detail(club=request.club, student_id=student_id)
    except Student.DoesNotExist as exc:
        raise Http404 from exc


def _card(request, student, message):
    context = _student_card_context(club=request.club, student=_student(request, student.id))
    context["operation_success"] = message
    response = render(request, "dashboard/students/_card.html", context)
    response["HX-Trigger"] = "studentUpdated"
    return response


def _subscription(request, student, subscription_id):
    return get_object_or_404(
        Subscription.objects.for_club(request.club).select_related("tariff", "club"),
        id=subscription_id,
        student=student,
        deleted_at__isnull=True,
    )


def _optional_int(value):
    return int(value) if value else None


@management_view_required
@require_http_methods(["GET", "POST"])
def subscription_correction(request, student_id, subscription_id):
    student = _student(request, student_id)
    subscription = _subscription(request, student, subscription_id)
    data = request.POST if request.method == "POST" else request.GET
    components = list(
        SubscriptionComponent.objects.for_club(request.club)
        .filter(
            subscription=subscription,
        )
        .select_related("training_type")
        .order_by("id")
    )
    try:
        component_id = _optional_int(data.get("component_id"))
    except ValueError as exc:
        raise Http404 from exc
    if component_id is None and len(components) == 1:
        component_id = components[0].id
    component = next((c for c in components if c.id == component_id), None)
    if component_id is not None and component is None:
        raise Http404
    expires_on = subscription_expires_on(subscription=subscription, club=request.club)
    finite = component is not None and component.entitlement_kind == "finite_credits"
    context = {
        "student": student,
        "subscription": subscription,
        "component": component,
        "finite": finite,
        "components": components,
        "component_id": component_id,
        "current_remaining": component.credits_left if finite else None,
        "current_expires_on": expires_on.isoformat() if expires_on else "",
        "initial_expires_on": data.get("initial_expires_on", expires_on.isoformat() if expires_on else ""),
        "remaining": data.get("remaining", str(component.credits_left) if finite else ""),
        "expires_on": data.get("expires_on", expires_on.isoformat() if expires_on else ""),
        "reason": data.get("reason", ""),
        "command_key": data.get("command_key") or uuid4().hex,
        "enabled": settings.STUDENT_ADMIN_CORRECTIONS_ENABLED,
    }
    if request.method == "POST":
        try:
            desired_expiry = date.fromisoformat(context["expires_on"]) if context["expires_on"] else None
            if context["expires_on"] == context["initial_expires_on"]:
                desired_expiry = None
            correct_subscription(
                club_id=request.club.id,
                actor_user_id=request.user.id,
                subscription_id=subscription.id,
                component_id=component_id,
                desired_remaining=int(context["remaining"]) if finite else None,
                desired_expires_on=desired_expiry,
                reason=context["reason"],
                command_key=context["command_key"],
                expected_fingerprint=data.get("expected_fingerprint", ""),
                channel="admin",
            )
            return _card(request, student, "Исправление сохранено. Реальные посещения и исходная цена сохранены.")
        except (ValueError, TypeError):
            context["error"] = "Укажите целое число занятий и корректную дату."
        except BusinessLogicError as error:
            context["error"] = str(error)
            context["error_code"] = error.code
            if error.code == "correction_stale_preview":
                context["error"] = (
                    "После открытия карточки данные изменились. Ваш ввод сохранён. "
                    "Проверьте текущий остаток и новое исправление."
                )
                context["initial_expires_on"] = context["current_expires_on"]
    try:
        state = get_subscription_correction_state(
            club_id=request.club.id,
            actor_user_id=request.user.id,
            subscription_id=subscription.id,
        )
        context["fingerprint"] = state["fingerprint"]
        context["can_save"] = bool(context["enabled"])
        raw_expiry = state["before"]["expires_at"]
        current_expiry = (
            club_localdate(request.club, datetime.fromisoformat(raw_expiry) - timedelta(microseconds=1)).isoformat()
            if raw_expiry else ""
        )
        context["current_expires_on"] = current_expiry
        if request.method == "GET" or context.get("error_code") == "correction_stale_preview":
            context["initial_expires_on"] = current_expiry
        if request.method == "GET":
            context["expires_on"] = current_expiry
        if finite:
            current = next(row for row in state["before"]["components"] if row["id"] == component_id)
            context["current_remaining"] = current["credits_left"]
            if request.method == "GET":
                context["remaining"] = str(current["credits_left"])
    except BusinessLogicError as error:
        context.setdefault("error", str(error))
        context["can_save"] = False
    return render(request, "dashboard/students/_subscription_correction.html", context)


@management_view_required
@require_http_methods(["GET", "POST"])
def attendance_record(request, student_id):
    student = _student(request, student_id)
    data = request.POST if request.method == "POST" else request.GET
    context = {
        "student": student,
        "enabled": settings.STUDENT_ADMIN_CORRECTIONS_ENABLED,
        "reason": data.get("reason", ""),
        "command_key": data.get("command_key") or uuid4().hex,
        "notify_parent": data.get("notify_parent") == "1",
    }
    try:
        day = date.fromisoformat(data.get("date") or club_localdate(request.club).isoformat())
        context["date"] = day.isoformat()
        context["today"] = club_localdate(request.club).isoformat()
        occurrences, components = get_student_attendance_options(club=request.club, student=student, day=day)
        context["occurrences"] = occurrences
        context["components"] = components
        schedule_id = _optional_int(data.get("schedule_id"))
        context["schedule_id"] = schedule_id
        entitlement = data.get("entitlement", "")
        context["entitlement"] = entitlement
        component = next((c for c in components if str(c.id) == entitlement), None)
        if request.method == "POST" and data.get("action") != "options":
            if schedule_id is None:
                raise ValueError
            allow_debt = entitlement == "debt"
            if component is None and entitlement.isdigit():
                component = (
                    SubscriptionComponent.objects.for_club(request.club)
                    .filter(
                        id=int(entitlement),
                        subscription__student=student,
                    )
                    .select_related("subscription")
                    .first()
                )
            if component is None and not allow_debt:
                raise ValueError
            args = dict(
                club_id=request.club.id,
                actor_user_id=request.user.id,
                student_id=student.id,
                schedule_id=schedule_id,
                checkin_date=day,
                subscription_id=component.subscription_id if component else None,
                component_id=component.id if component else None,
                allow_debt=allow_debt,
            )
            if data.get("action") == "apply":
                receipt = record_student_attendance(
                    **args,
                    expected_fingerprint=data.get("expected_fingerprint", ""),
                    command_key=context["command_key"],
                    reason=context["reason"],
                    channel="admin",
                    notify_parent=context["notify_parent"],
                )
                return _card(request, student, f"Посещение {receipt.checkin.date:%d.%m.%Y} отмечено.")
            preview = preview_student_attendance(**args)
            context["preview"] = preview
            context["can_apply"] = bool(context["enabled"])
            if component and component.entitlement_kind == "finite_credits":
                selected_state = next(row for row in preview["evidence"]["financial"]["state"]["components"]
                                      if row["id"] == component.id)
                context["before_remaining"] = selected_state["credits_left"]
                context["after_remaining"] = selected_state["credits_left"] - 1
            salary = preview["evidence"]["financial"]["salary_snapshot"]
            if component and salary["payout_policy_snapshot"] == "on_checkin":
                if salary["rate_percent_snapshot"] is None:
                    context["can_apply"] = False
                    context["error"] = "Для тренера не задана ставка. Сначала уточните начисление."
                else:
                    context["salary_amount"] = (
                        Decimal(salary["subscription_price_snapshot"]) * Decimal(salary["rate_percent_snapshot"]) / 100
                    )
    except (ValueError, TypeError):
        context["error"] = "Выберите дату, занятие и точный абонемент или занятие в долг."
    except BusinessLogicError as error:
        context["error"] = str(error)
        context["can_apply"] = False
        if error.code == "attendance_exact_drop_in_required":
            context["exact_session_url"] = (
                reverse("session-detail", kwargs={"schedule_id": schedule_id}) + f"?date={day}"
            )
    return render(request, "dashboard/students/_attendance_record.html", context)


@management_view_required
@require_http_methods(["GET", "POST"])
def attendance_cancel(request, student_id, checkin_id):
    student = _student(request, student_id)
    checkin = get_object_or_404(
        Checkin.objects.for_club(request.club).select_related("schedule", "training_type"),
        id=checkin_id,
        student=student,
    )
    context = {
        "student": student,
        "checkin": checkin,
        "reason": request.POST.get("reason", ""),
        "command_key": request.POST.get("command_key") or uuid4().hex,
    }
    if request.method == "POST":
        try:
            cancel_student_attendance(
                club_id=request.club.id,
                actor_user_id=request.user.id,
                student_id=student.id,
                checkin_id=checkin.id,
                command_key=context["command_key"],
                reason=context["reason"],
                channel="admin",
            )
            return _card(request, student, f"Отметка за {checkin.date:%d.%m.%Y} отменена.")
        except BusinessLogicError as error:
            context["error"] = str(error)
    return render(request, "dashboard/students/_attendance_cancel.html", context)


@management_view_required
@require_http_methods(["GET"])
def operation_history(request, student_id):
    student = _student(request, student_id)
    kind = request.GET.get("kind", "attendance")
    if kind not in {"attendance", "payments", "corrections"}:
        raise Http404
    rows = get_student_operation_history(
        club=request.club, student_id=student.id, kind=kind, page=request.GET.get("page", 1)
    )
    if kind == "corrections":
        for row in rows:
            if hasattr(row, "balance_delta"):
                row.balance_changes = []
                for before in row.before.get("components", []):
                    after = next((c for c in row.after.get("components", []) if c["id"] == before["id"]), None)
                    if after and before.get("credits_left") != after.get("credits_left"):
                        row.balance_changes.append({"before": before["credits_left"], "after": after["credits_left"]})
                row.expiry_changed = row.before.get("expires_at") != row.after.get("expires_at")
                for side in ("before", "after"):
                    raw = getattr(row, side).get("expires_at")
                    value = (
                        club_localdate(request.club, datetime.fromisoformat(raw) - timedelta(microseconds=1))
                        if raw else None
                    )
                    setattr(row, f"{side}_expiry", value)
    return render(
        request, "dashboard/students/_operation_history.html", {"student": student, "kind": kind, "history_page": rows}
    )


@management_view_required
@require_http_methods(["GET", "POST"])
def subscription_renew(request, student_id, subscription_id):
    from apps.billing.service_modules.bank_orders import create_bank_payment_order
    from apps.billing.service_modules.renewals import (
        create_manual_subscription_renewal,
        get_renewal_offer,
    )
    from apps.htmx_admin.views.billing import _online_creation_available

    student = _student(request, student_id)
    subscription = _subscription(request, student, subscription_id)
    renewal_offer = get_renewal_offer(
        club_id=request.club.id,
        source_tariff=subscription.tariff,
    )
    context = {
        "student": student,
        "subscription": subscription,
        "renewal_offer": renewal_offer,
        "command_key": request.POST.get("command_key") or uuid4().hex,
        "online_available": _online_creation_available(),
    }
    if not renewal_offer.is_available:
        context["error"] = "Текущее предложение продления недоступно. Обновите карточку абонемента."
    if request.method == "POST":
        method = request.POST.get("payment_method", "")
        try:
            expected_target_tariff_id = _optional_int(request.POST.get("expected_target_tariff_id"))
            expected_target_price = request.POST.get("expected_target_price") or None
            if method == "online":
                order = create_bank_payment_order(
                    club_id=request.club.id,
                    student_id=student.id,
                    tariff_id=None,
                    renewed_from_subscription_id=subscription.id,
                    source=BankPaymentOrder.Source.OWNER
                    if request._membership.role == "owner"
                    else BankPaymentOrder.Source.ADMIN,
                    created_by_id=request.user.id,
                    command_idempotency_key=context["command_key"],
                    expected_target_tariff_id=expected_target_tariff_id,
                    expected_target_price=expected_target_price,
                )
                return HttpResponse(
                    status=204, headers={"HX-Redirect": f"/dashboard/billing/payments/?bank_order={order.id}"}
                )
            payment = create_manual_subscription_renewal(
                club_id=request.club.id,
                student_id=student.id,
                renewed_from_subscription_id=subscription.id,
                payment_method=method,
                recorded_by_id=request.user.id,
                command_idempotency_key=context["command_key"],
                expected_target_tariff_id=expected_target_tariff_id,
                expected_target_price=expected_target_price,
            )
            return _card(request, student, f"Продление №{payment.id} записано. Оплата ожидает проверки.")
        except (TypeError, ValueError):
            context["error"] = "Некорректное предложение продления. Обновите карточку."
        except BusinessLogicError as error:
            context["error"] = str(error)
            try:
                context["renewal_offer"] = get_renewal_offer(
                    club_id=request.club.id,
                    source_tariff=subscription.tariff,
                )
            except BusinessLogicError:
                context["renewal_offer"] = renewal_offer
    return render(request, "dashboard/students/_subscription_renew.html", context)
