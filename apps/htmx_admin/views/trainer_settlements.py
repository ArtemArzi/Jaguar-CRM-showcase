"""Thin forms for confirmed actual payouts and explicit historical reconciliation."""

from datetime import date
from uuid import uuid4

from django.conf import settings
from django.shortcuts import get_object_or_404, render
from django.views.decorators.http import require_http_methods

from apps.clubs.timezones import club_localdate
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required
from apps.trainers.models import Trainer, TrainerSettlementEntry, TrainerSettlementReconciliation
from apps.trainers.settlement_selectors import get_trainer_settlement_summary
from apps.trainers.settlement_services import record_trainer_settlement, resolve_trainer_settlement


def settlement_context(request, trainer, date_from, date_to):
    summary = get_trainer_settlement_summary(
        club=request.club, trainer_id=trainer.id, date_from=date_from, date_to=date_to
    )
    entries = (
        TrainerSettlementEntry.objects.for_club(request.club)
        .filter(trainer=trainer)
        .select_related("actor", "reversal")
        .order_by("-effective_on", "-id")[:25]
    )
    cases = (
        TrainerSettlementReconciliation.objects.for_club(request.club)
        .filter(
            trainer=trainer,
            resolution__isnull=True,
        )
        .order_by("effective_on", "id")
    )
    return {
        "settlement": summary,
        "settlement_entries": entries,
        "settlement_cases": cases,
        "settlements_enabled": settings.TRAINER_SETTLEMENTS_ENABLED,
    }


@management_view_required
@require_http_methods(["GET", "POST"])
def settlement_form(request, trainer_id):
    trainer = get_object_or_404(Trainer.objects.for_club(request.club), id=trainer_id)
    data = request.POST if request.method == "POST" else request.GET
    kind = data.get("kind", "payout")
    if kind not in TrainerSettlementEntry.Kind.values:
        from django.http import Http404

        raise Http404
    today = club_localdate(request.club)
    context = {
        "trainer": trainer,
        "kind": kind,
        "effective_on": data.get("effective_on", str(today)),
        "reason": data.get("reason", ""),
        "value": data.get("value", ""),
        "source_key": data.get("source_key") or uuid4().hex,
        "payment_method": data.get("payment_method", "cash"),
        "confirm_advance": data.get("confirm_advance") == "1",
        "today": str(today),
        "settlements_enabled": settings.TRAINER_SETTLEMENTS_ENABLED,
        "title": dict(TrainerSettlementEntry.Kind.choices)[kind],
    }
    reversal = None
    if data.get("reversal_of_id"):
        reversal = get_object_or_404(
            TrainerSettlementEntry.objects.for_club(request.club),
            id=data["reversal_of_id"],
            trainer=trainer,
            kind="payout",
        )
    context["reversal"] = reversal
    try:
        day = date.fromisoformat(context["effective_on"])
        if request.method == "POST" and data.get("action") != "options":
            entry = record_trainer_settlement(
                club_id=request.club.id,
                actor_user_id=request.user.id,
                trainer_id=trainer.id,
                kind=kind,
                effective_on=day,
                reason=context["reason"],
                source_namespace="admin",
                source_key=context["source_key"],
                balance_delta=context["value"] if kind in {"opening", "opening_correction"} else None,
                amount=context["value"] if kind == "payout" else None,
                reversal_of_id=reversal.id if reversal else None,
                payment_method=context["payment_method"] if kind == "payout" else "",
                confirm_advance=context["confirm_advance"],
                expected_fingerprint=data.get("expected_fingerprint"),
            )
            context["success"] = f"{entry.get_kind_display()} №{entry.id} записана."
    except (ValueError, TypeError):
        day = today
        context["error"] = "Укажите корректную дату."
    except BusinessLogicError as error:
        context["error"] = str(error)
    context["summary"] = get_trainer_settlement_summary(
        club=request.club, trainer_id=trainer.id, date_from=day, date_to=day
    )
    return render(request, "dashboard/trainers/_settlement_form.html", context)


@management_view_required
@require_http_methods(["GET", "POST"])
def settlement_reconciliation(request, trainer_id, case_id):
    trainer = get_object_or_404(Trainer.objects.for_club(request.club), id=trainer_id)
    case = get_object_or_404(
        TrainerSettlementReconciliation.objects.for_club(request.club).select_related("opening"),
        id=case_id,
        trainer=trainer,
    )
    data = request.POST if request.method == "POST" else request.GET
    context = {
        "trainer": trainer,
        "case": case,
        "reason": data.get("reason", ""),
        "source_key": data.get("source_key") or uuid4().hex,
        "action": data.get("action", "already_included"),
        "value": data.get("value", str(case.suggested_delta)),
        "effective_on": data.get("effective_on", str(club_localdate(request.club))),
    }
    if request.method == "POST":
        try:
            resolve_trainer_settlement(
                club_id=request.club.id,
                actor_user_id=request.user.id,
                case_id=case.id,
                action=context["action"],
                reason=context["reason"],
                source_namespace="admin",
                source_key=context["source_key"],
                balance_delta=context["value"] if context["action"] == "adjust_opening" else None,
                effective_on=date.fromisoformat(context["effective_on"])
                if context["action"] == "adjust_opening"
                else None,
            )
            context["success"] = "Решение сверки сохранено."
        except (ValueError, TypeError):
            context["error"] = "Укажите корректную дату и сумму."
        except BusinessLogicError as error:
            context["error"] = str(error)
    return render(request, "dashboard/trainers/_settlement_reconciliation.html", context)
