"""Student-card adapters to the common refund accounting owner."""

from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import uuid4

from django.db.models import Sum
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from apps.billing.models import Payment, PaymentRefund
from apps.billing.refund_services import complete_payment_refund_payroll, record_manual_payment_refund
from apps.clubs.timezones import club_localdate
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required
from apps.htmx_admin.views.student_operations import _card, _student


@management_view_required
@require_http_methods(["GET", "POST"])
def student_payment_refund(request, student_id, payment_id):
    student = _student(request, student_id)
    payment = get_object_or_404(Payment.objects.for_club(request.club), id=payment_id, student=student)
    data = request.POST if request.method == "POST" else {}
    context = dict(
        student=student,
        payment=payment,
        title="Записать возврат",
        amount=data.get("amount", ""),
        accounting_date=data.get("accounting_date", club_localdate(request.club).isoformat()),
        reason=data.get("reason", ""),
        idempotency_key=data.get("idempotency_key", str(uuid4())),
        entitlement_action=data.get("entitlement_action", "kept_partial"),
        legacy_enrollment_action=data.get("legacy_enrollment_action", ""),
        legacy_enrollment_id=data.get("legacy_enrollment_id", ""),
    )
    if request.method == "POST":
        try:
            record_manual_payment_refund(
                club_id=request.club.id,
                actor_user_id=request.user.id,
                payment_id=payment.id,
                subscription_id=payment.subscription_id,
                amount=Decimal(context["amount"]),
                accounting_date=date.fromisoformat(context["accounting_date"]),
                reason=context["reason"],
                idempotency_key=context["idempotency_key"],
                entitlement_action=context["entitlement_action"],
                legacy_enrollment_action=context["legacy_enrollment_action"] or None,
                legacy_enrollment_id=int(context["legacy_enrollment_id"]) if context["legacy_enrollment_id"] else None,
            )
            return _card(request, student, "Возврат записан. Исходная оплата сохранена в истории.")
        except BusinessLogicError as exc:
            context["error"] = exc.message
        except (ValueError, InvalidOperation, TypeError):
            context["error"] = "Проверьте сумму, дату и номер зачисления."
    refunds = PaymentRefund.objects.for_club(request.club).filter(payment=payment).order_by("-id")
    context["refunds"] = refunds
    context["available_amount"] = payment.amount - (refunds.aggregate(total=Sum("amount"))["total"] or Decimal("0"))
    return render(request, "dashboard/students/_payment_refund.html", context)


@management_view_required
@require_http_methods(["POST"])
def student_refund_payroll(request, student_id, refund_id):
    student = _student(request, student_id)
    refund = get_object_or_404(PaymentRefund.objects.for_club(request.club), id=refund_id, payment__student=student)
    context = dict(
        student=student,
        refund=refund,
        title="Компенсация комиссии",
        effective_date=request.POST.get("effective_date", ""),
    )
    try:
        complete_payment_refund_payroll(
            club_id=request.club.id,
            refund_id=refund.id,
            actor_user_id=request.user.id,
            effective_date=date.fromisoformat(context["effective_date"]),
        )
        return _card(request, student, "Компенсация комиссии записана.")
    except BusinessLogicError as exc:
        context["error"] = exc.message
    except ValueError:
        context["error"] = "Укажите открытую дату не раньше возврата."
    return render(request, "dashboard/students/_refund_payroll.html", context)
