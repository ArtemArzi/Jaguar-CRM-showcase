import logging
import re
import uuid
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render

from apps.billing.recognition import payment_recognition_date
from apps.clubs.models import Location
from apps.clubs.timezones import club_localdate
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required
from apps.trainers.models import TrainerEarningAdjustment
from apps.trainers.selectors import (
    get_trainer_by_id,
    get_trainer_earnings,
    get_trainer_earnings_summary,
    get_trainer_payroll_adjustments,
    get_trainer_revenue_summary,
    get_trainers,
    get_trainers_with_stats,
    resolve_trainer_rate,
)
from apps.trainers.services import (
    close_trainer_payroll_period,
    correct_trainer_earning,
    create_trainer,
    get_trainer_payroll_closes_for_range,
    update_trainer,
)

logger = logging.getLogger(__name__)

PHONE_RE = re.compile(r"^\+?\d{10,15}$")

ADJUSTMENT_KIND_LABELS = {
    TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER: "Передача пакета",
    TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT: "Ручная корректировка",
    TrainerEarningAdjustment.Kind.REFUND: "Возврат оплаты",
}
ADJUSTMENT_DIRECTION_LABELS = {
    TrainerEarningAdjustment.Direction.CREDIT: "Начисление",
    TrainerEarningAdjustment.Direction.DEBIT: "Списание",
    TrainerEarningAdjustment.Direction.INFO: "Информация",
}


def _has_manual_correction_debit(*, earning, adjustments) -> bool:
    return any(
        adjustment.kind == TrainerEarningAdjustment.Kind.MANUAL_ADJUSTMENT
        and adjustment.direction == TrainerEarningAdjustment.Direction.DEBIT
        and adjustment.trainer_id == earning.trainer_id
        for adjustment in adjustments
    )


def _format_salary_adjustment(adjustment) -> dict:
    return {
        "kind": ADJUSTMENT_KIND_LABELS.get(adjustment.kind, adjustment.get_kind_display()),
        "direction": ADJUSTMENT_DIRECTION_LABELS.get(
            adjustment.direction,
            adjustment.get_direction_display(),
        ),
        "payable_delta": adjustment.payable_amount_delta,
        "affects_payroll": adjustment.affects_payroll,
        "reason": adjustment.reason,
        "trainer_name": str(adjustment.trainer),
        "counterparty_name": (
            str(adjustment.counterparty_trainer)
            if adjustment.counterparty_trainer_id
            else ""
        ),
        "created_by": (
            adjustment.created_by.get_full_name()
            or adjustment.created_by.get_username()
            if adjustment.created_by_id
            else ""
        ),
    }


def _salary_source_key(*, checkin_id: int | None, payment_id: int | None) -> tuple[str, int] | None:
    if checkin_id is not None:
        return ("checkin", checkin_id)
    if payment_id is not None:
        return ("payment", payment_id)
    return None


def _closed_period_for_date(source_date, payroll_closes):
    if source_date is None:
        return None
    for close in payroll_closes:
        if close.period_start <= source_date <= close.period_end:
            return close
    return None


def _build_salary_audit_rows(*, club, earnings, payroll_adjustments=(), payroll_closes=()) -> list[dict]:
    rows = []
    represented_sources: set[tuple[str, int]] = set()
    for earning in earnings:
        checkin = earning.checkin
        payment = earning.payment
        package_transfer = None
        raw_adjustments = []
        adjustment_rows = []
        source_key = _salary_source_key(
            checkin_id=earning.checkin_id,
            payment_id=earning.payment_id,
        )
        if source_key is not None:
            represented_sources.add(source_key)

        if checkin is not None:
            package_transfers = getattr(checkin, "package_transfer_adjustments", [])
            package_transfer = package_transfers[0] if package_transfers else None
            raw_adjustments = list(getattr(checkin, "salary_audit_adjustments", []))
            adjustment_rows = [
                _format_salary_adjustment(adjustment)
                for adjustment in raw_adjustments
            ]
        elif payment is not None:
            raw_adjustments = list(getattr(payment, "salary_audit_adjustments", []))
            adjustment_rows = [
                _format_salary_adjustment(adjustment)
                for adjustment in raw_adjustments
            ]

        source_date = None
        if checkin is not None:
            source_date = checkin.date
        elif payment is not None and payment.verified_at:
            source_date = payment_recognition_date(payment=payment, club=club)
        closed_period = _closed_period_for_date(source_date, payroll_closes)

        rows.append({
            "id": earning.id,
            "date": source_date,
            "source": earning.get_earning_source_display(),
            "earning_type": earning.get_earning_type_display(),
            "amount": earning.amount,
            "rate_percent": earning.rate_percent,
            "checkin_id": checkin.id if checkin else None,
            "payment_id": payment.id if payment else None,
            "schedule_name": checkin.schedule.group_name if checkin and checkin.schedule_id else "",
            "can_correct": closed_period is None and not _has_manual_correction_debit(
                earning=earning,
                adjustments=raw_adjustments,
            ),
            "closed_period": closed_period,
            "package_owner_name": (
                str(package_transfer.counterparty_trainer)
                if package_transfer is not None and package_transfer.counterparty_trainer_id
                else ""
            ),
            "package_transfer_reason": package_transfer.reason if package_transfer else "",
            "adjustments": adjustment_rows,
        })
    for adjustment in payroll_adjustments:
        source_checkin = adjustment.source_checkin
        source_key = _salary_source_key(
            checkin_id=adjustment.source_checkin_id,
            payment_id=adjustment.source_payment_id,
        )
        if source_key is not None and source_key in represented_sources:
            continue
        closed_period = _closed_period_for_date(adjustment.effective_date, payroll_closes)
        rows.append({
            "id": None,
            "date": adjustment.effective_date,
            "source": "Корректировка",
            "earning_type": ADJUSTMENT_KIND_LABELS.get(
                adjustment.kind,
                adjustment.get_kind_display(),
            ),
            "amount": adjustment.payable_amount_delta,
            "rate_percent": None,
            "checkin_id": adjustment.source_checkin_id,
            "payment_id": adjustment.source_payment_id,
            "schedule_name": (
                source_checkin.schedule.group_name
                if source_checkin is not None and source_checkin.schedule_id
                else ""
            ),
            "can_correct": False,
            "closed_period": closed_period,
            "package_owner_name": "",
            "package_transfer_reason": "",
            "adjustments": [_format_salary_adjustment(adjustment)],
        })
    return rows


def _get_earning_for_correction(*, club, earning_id: int):
    from apps.trainers.models import TrainerEarning

    return (
        TrainerEarning.objects.for_club(club)
        .select_related("trainer", "checkin", "checkin__schedule", "payment")
        .filter(id=earning_id, cancelled=False)
        .first()
    )


def _safe_rate(value: str | None, default: float = 20.0) -> float:
    try:
        v = float(value or default)
    except (ValueError, TypeError):
        return default
    return max(0.0, min(100.0, round(v, 2)))


def _parse_rate_or_none(raw: str | None) -> Decimal | None:
    """Parse rate from POST. Returns:
      - None if input is empty / whitespace (caller should 'skip' — keep existing)
      - Decimal clamped to [0, 100]
    Raises ValueError on unparseable non-empty input.
    """
    from decimal import Decimal, InvalidOperation

    if raw is None:
        return None
    s = raw.strip().replace(",", ".")
    if not s:
        return None
    try:
        v = Decimal(s)
    except InvalidOperation:
        raise ValueError(f"Некорректное значение ставки: {raw!r}")
    if v.is_nan() or v < Decimal("0") or v > Decimal("100"):
        raise ValueError(f"Ставка должна быть 0..100%: {raw!r}")
    return v


@management_view_required
def trainer_create(request: HttpRequest) -> HttpResponse:
    from apps.billing.models import TrainingType
    locations = list(Location.objects.filter(club=request.club))
    training_types = list(
        TrainingType.objects.for_club(request.club.id).filter(is_active=True)
    )

    def _default_grid():
        return [
            {
                "location": loc,
                "rates": [
                    {"training_type": tt, "percent": _kind_default(tt.kind)}
                    for tt in training_types
                ],
            }
            for loc in locations
        ]

    def _render_form_error(error: str):
        response = render(request, "dashboard/trainers/_create_form.html", {
            "locations": locations,
            "rates_grid": _default_grid(),
            "error": error,
            "form_data": {
                "first_name": (request.POST.get("first_name") or "").strip(),
                "last_name": (request.POST.get("last_name") or "").strip(),
                "phone": (request.POST.get("phone") or "").strip(),
            },
        })
        if request.htmx:
            response["HX-Retarget"] = "#slide-over"
        return response

    if request.method == "GET":
        return render(request, "dashboard/trainers/_create_form.html", {
            "locations": locations,
            "rates_grid": _default_grid(),
        })

    # POST
    first_name = (request.POST.get("first_name") or "").strip()
    last_name = (request.POST.get("last_name") or "").strip()
    phone = (request.POST.get("phone") or "").strip()

    if not first_name or not last_name:
        return _render_form_error("Имя и фамилия обязательны")

    if phone and not PHONE_RE.match(phone):
        return _render_form_error("Неверный формат телефона")


    location_data: list[dict] = []
    try:
        for loc in locations:
            if not request.POST.get(f"location_{loc.id}"):
                continue
            # New shape: percent_{loc}_{type}. Fallback to legacy rate_{kind}_{loc}.
            rates: list[dict] = []
            for tt in training_types:
                key_new = f"percent_{loc.id}_{tt.id}"
                val = request.POST.get(key_new)
                if val is None:
                    # legacy fallback keys
                    if tt.kind == TrainingType.Kind.GROUP:
                        val = request.POST.get(f"rate_group_{loc.id}")
                    elif tt.kind == TrainingType.Kind.PERSONAL:
                        val = request.POST.get(f"rate_personal_{loc.id}")
                    elif tt.kind == TrainingType.Kind.MINI_GROUP:
                        val = request.POST.get(f"rate_mini_{loc.id}")
                percent = _parse_rate_or_none(val)
                if percent is None:
                    percent = _kind_default(tt.kind)
                rates.append({
                    "training_type_id": tt.id,
                    "percent": percent,
                })
            location_data.append({"location_id": loc.id, "rates": rates})
    except ValueError as e:
        return _render_form_error(str(e))

    try:
        create_trainer(
            club_id=request.club.id,
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            locations=location_data or None,
        )
    except BusinessLogicError as e:
        return _render_form_error(str(e))

    today = club_localdate(request.club)
    list_from = today.replace(day=1)
    trainers = get_trainers_with_stats(
        club=request.club,
        date_from=list_from,
        date_to=today,
    )
    response = render(request, "dashboard/trainers/list.html#content", {
        "page_title": "Тренеры",
        "trainers": trainers,
        "date_from": list_from,
        "date_to": today,
        "active_tab": "list",
    })
    response["HX-Trigger"] = "closeSlideOver"
    return response


@management_view_required
def trainer_list(request: HttpRequest) -> HttpResponse:
    today = club_localdate(request.club)
    date_from_str = request.GET.get("date_from", "")
    date_to_str = request.GET.get("date_to", "")


    try:
        list_from = date.fromisoformat(date_from_str) if date_from_str else today.replace(day=1)
    except ValueError:
        list_from = today.replace(day=1)
    try:
        list_to = date.fromisoformat(date_to_str) if date_to_str else today
    except ValueError:
        list_to = today

    trainers = get_trainers_with_stats(
        club=request.club,
        date_from=list_from,
        date_to=list_to,
    )
    context = {
        "page_title": "Тренеры",
        "trainers": trainers,
        "date_from": list_from,
        "date_to": list_to,
        "active_tab": "list",
    }
    if request.htmx:
        return render(request, "dashboard/trainers/list.html#content", context)
    return render(request, "dashboard/trainers/list.html", context)


@management_view_required
def trainer_sessions(request: HttpRequest) -> HttpResponse:
    """Sub-tab on /dashboard/trainers/: flat list of training sessions
    (unique schedule × date) with attendee counts, filterable by
    trainer, schedule, and date range."""
    from apps.trainers.selectors import get_club_sessions, get_schedules_for_club

    today = club_localdate(request.club)
    date_from_str = request.GET.get("date_from", "")
    date_to_str = request.GET.get("date_to", "")
    trainer_id_str = request.GET.get("trainer_id", "")
    schedule_id_str = request.GET.get("schedule_id", "")

    try:
        d_from = date.fromisoformat(date_from_str) if date_from_str else today.replace(day=1)
    except ValueError:
        d_from = today.replace(day=1)
    try:
        d_to = date.fromisoformat(date_to_str) if date_to_str else today
    except ValueError:
        d_to = today
    if d_from > d_to:
        d_from, d_to = d_to, d_from

    trainer_id: int | None = None
    if trainer_id_str:
        try:
            trainer_id = int(trainer_id_str)
        except ValueError:
            trainer_id = None

    schedule_id: int | None = None
    if schedule_id_str:
        try:
            schedule_id = int(schedule_id_str)
        except ValueError:
            schedule_id = None

    sessions = get_club_sessions(
        club=request.club,
        date_from=d_from,
        date_to=d_to,
        trainer_id=trainer_id,
        schedule_id=schedule_id,
    )
    total_attendees = sum(s["attendees"] for s in sessions)
    session_count = len(sessions)
    avg_attendees = round(total_attendees / session_count, 1) if session_count else 0

    # Filter options — all active trainers + all schedules for the club
    from apps.trainers.models import Trainer as _Trainer

    all_trainers = list(
        _Trainer.objects.for_club(request.club)
        .filter(is_active=True)
        .order_by("first_name")
    )
    all_schedules = list(get_schedules_for_club(club=request.club))

    context = {
        "page_title": "Тренировки",
        "sessions": sessions,
        "date_from": d_from,
        "date_to": d_to,
        "current_trainer_id": trainer_id,
        "current_schedule_id": schedule_id,
        "all_trainers": all_trainers,
        "all_schedules": all_schedules,
        "total_attendees": total_attendees,
        "session_count": session_count,
        "avg_attendees": avg_attendees,
        "active_tab": "sessions",
    }
    if request.htmx:
        return render(request, "dashboard/trainers/sessions.html#content", context)
    return render(request, "dashboard/trainers/sessions.html", context)


@management_view_required
def trainer_detail(request: HttpRequest, trainer_id: int) -> HttpResponse:
    from apps.students.models import Student
    from apps.trainers.models import Trainer

    try:
        trainer = get_trainer_by_id(club=request.club, trainer_id=trainer_id)
    except Trainer.DoesNotExist:
        raise Http404

    today = club_localdate(request.club)
    date_from_str = request.GET.get("date_from", "")
    date_to_str = request.GET.get("date_to", "")

    from apps.trainers.settlement_selectors import trainer_period_presets

    presets = trainer_period_presets(club=request.club)
    preset = next((p for p in presets if p["key"] == request.GET.get("period")), None)
    if preset:
        date_from_str, date_to_str = str(preset["start"]), str(preset["end"])

    try:
        salary_from = date.fromisoformat(date_from_str) if date_from_str else today.replace(day=1)
    except ValueError:
        salary_from = today.replace(day=1)
    try:
        salary_to = date.fromisoformat(date_to_str) if date_to_str else today
    except ValueError:
        salary_to = today
    if salary_from > salary_to:
        salary_from, salary_to = salary_to, salary_from

    summary = get_trainer_earnings_summary(
        club=request.club, trainer_id=trainer_id, date_from=salary_from, date_to=salary_to,
    )
    earnings = list(
        get_trainer_earnings(
            club=request.club,
            trainer_id=trainer_id,
            date_from=salary_from,
            date_to=salary_to,
        )
    )
    payroll_adjustments = list(
        get_trainer_payroll_adjustments(
            club=request.club,
            trainer_id=trainer_id,
            date_from=salary_from,
            date_to=salary_to,
        )
    )
    payroll_closes = list(
        get_trainer_payroll_closes_for_range(
            club_id=request.club.id,
            period_start=salary_from,
            period_end=salary_to,
        )
    )
    revenue = get_trainer_revenue_summary(
        club=request.club, trainer_id=trainer_id, date_from=salary_from, date_to=salary_to,
    )

    # Add human-readable labels for earning types
    earning_labels = {
        "group": "Групповые",
        "personal": "Персональные",
        "mini_group": "Мини-группы",
        "manual_adjustment": "Ручная корректировка",
        "refund": "Возврат оплаты",
    }
    for type_key, data in summary.get("by_type", {}).items():
        data["label"] = earning_labels.get(type_key, type_key)

    # Count active students linked to this trainer's active schedules.
    from apps.attendance.models import ScheduleEnrollment

    student_count = (
        Student.objects.for_club(request.club)
        .filter(
            status="active",
            schedule_enrollments__schedule__trainer=trainer,
            schedule_enrollments__schedule__is_active=True,
            schedule_enrollments__status=ScheduleEnrollment.Status.ACTIVE,
        )
        .distinct()
        .count()
    )

    # Conversion: trial students who checked in with this trainer → how many became active
    from apps.attendance.models import Checkin
    trial_student_ids = set(
        Checkin.objects.filter(
            club=request.club, trainer=trainer,
            date__gte=salary_from, date__lte=salary_to,
            deleted_at__isnull=True,
            student__status__in=["trial", "active"],
        ).values_list("student_id", flat=True).distinct()
    )
    if trial_student_ids:
        converted = Student.objects.for_club(request.club).filter(
            id__in=trial_student_ids, status="active",
        ).count()
        total_trials = len(trial_student_ids)
        conversion_rate = round(converted * 100 / total_trials) if total_trials else 0
    else:
        converted = 0
        total_trials = 0
        conversion_rate = 0

    # Average attendance: how many students per actual training session.
    # A "session" = unique (schedule, date) pair — so a day with 2 groups
    # (morning BJJ + evening Muay Thai) counts as 2 sessions, not 1.
    session_count = (
        Checkin.objects.filter(
            club=request.club, trainer=trainer,
            date__gte=salary_from, date__lte=salary_to,
            deleted_at__isnull=True,
        )
        .values("schedule_id", "date")
        .distinct()
        .count()
    )
    total_checkins = Checkin.objects.filter(
        club=request.club, trainer=trainer,
        date__gte=salary_from, date__lte=salary_to,
        deleted_at__isnull=True,
    ).count()
    avg_attendance = round(total_checkins / session_count, 1) if session_count else 0
    # Kept name `session_dates` in context for template backwards-compat,
    # but it now carries the session count (schedule × date), not date count.
    session_dates = session_count

    from apps.billing.models import TrainingType
    from apps.trainers.models import TrainerLocation, TrainerRate

    # Only show rates for ACTIVE training types on the trainer card.
    # Rates for disabled types are preserved in DB but hidden from the UI.
    trainer_rates_qs = (
        TrainerRate.objects.for_club(request.club)
        .filter(trainer=trainer, training_type__is_active=True)
        .select_related("location", "training_type")
        .order_by("location__name", "training_type__name")
    )
    # Find rate gaps that can actually block or skip salary calculation. Group
    # sale salary can fall back to any location for the same training type;
    # personal/mini-group check-in salary still requires the exact pair.
    existing_pairs: set[tuple[int, int]] = {
        (r.location_id, r.training_type_id) for r in trainer_rates_qs
    }
    existing_training_type_ids: set[int] = {r.training_type_id for r in trainer_rates_qs}
    trainer_locations = list(
        TrainerLocation.objects.for_club(request.club)
        .filter(trainer=trainer)
        .select_related("location")
    )
    club_training_types = list(
        TrainingType.objects.for_club(request.club).filter(is_active=True)
    )
    missing_rates: list[dict] = []
    for tl in trainer_locations:
        for tt in club_training_types:
            if (tl.location_id, tt.id) in existing_pairs:
                continue
            if tt.kind == TrainingType.Kind.GROUP and tt.id in existing_training_type_ids:
                continue
            missing_rates.append({
                "location_name": tl.location.name,
                "training_type_name": tt.name,
            })

    context = {
        "page_title": f"{trainer.first_name} {trainer.last_name}",
        "trainer": trainer,
        "trainer_rates": trainer_rates_qs,
        "missing_rates": missing_rates,
        "summary": summary,
        "salary_audit_rows": _build_salary_audit_rows(
            club=request.club,
            earnings=earnings,
            payroll_adjustments=payroll_adjustments,
            payroll_closes=payroll_closes,
        ),
        "payroll_closes": payroll_closes,
        "is_payroll_period_closed": bool(payroll_closes),
        "revenue": revenue,
        "student_count": student_count,
        "date_from": salary_from,
        "date_to": salary_to,
        "conversion_rate": conversion_rate,
        "converted": converted,
        "total_trials": total_trials,
        "avg_attendance": avg_attendance,
        "session_dates": session_dates,
        "total_checkins": total_checkins,
    }
    from apps.htmx_admin.views.trainer_settlements import settlement_context
    from apps.trainers.models import TrainerSettlementEntry

    if settings.TRAINER_SETTLEMENTS_ENABLED or TrainerSettlementEntry.objects.for_club(request.club).filter(
        trainer=trainer,
    ).exists():
        context.update(settlement_context(request, trainer, salary_from, salary_to))
        context["settlement_presets"] = presets
    if request.htmx:
        return render(request, "dashboard/trainers/detail.html#content", context)
    return render(request, "dashboard/trainers/detail.html", context)


@management_view_required
def trainer_payroll_close_period(request: HttpRequest, trainer_id: int) -> HttpResponse:
    from apps.trainers.models import Trainer

    if request.method != "POST":
        raise Http404
    try:
        get_trainer_by_id(club=request.club, trainer_id=trainer_id)
    except Trainer.DoesNotExist:
        raise Http404
    try:
        period_start = date.fromisoformat(request.POST.get("date_from") or "")
        period_end = date.fromisoformat(request.POST.get("date_to") or "")
    except ValueError:
        return HttpResponse("Некорректный период выплат", status=400)
    reason = (request.POST.get("reason") or "").strip()
    try:
        close_trainer_payroll_period(
            club_id=request.club.id,
            period_start=period_start,
            period_end=period_end,
            reason=reason,
            actor_user_id=request.user.id,
        )
    except BusinessLogicError as exc:
        return HttpResponse(exc.message, status=400)

    response = HttpResponse(status=204)
    response["HX-Redirect"] = (
        f"/dashboard/trainers/{trainer_id}/"
        f"?date_from={period_start.isoformat()}&date_to={period_end.isoformat()}"
    )
    return response


@management_view_required
def trainer_edit(request: HttpRequest, trainer_id: int) -> HttpResponse:
    from apps.trainers.models import Trainer

    try:
        trainer = get_trainer_by_id(club=request.club, trainer_id=trainer_id)
    except Trainer.DoesNotExist:
        raise Http404

    if request.method == "GET":
        return render(request, "dashboard/trainers/_edit_form.html", {"trainer": trainer})

    first_name = (request.POST.get("first_name") or "").strip()
    last_name = (request.POST.get("last_name") or "").strip()
    phone = (request.POST.get("phone") or "").strip()
    is_active = request.POST.get("is_active") == "1"

    if not first_name or not last_name:
        return render(request, "dashboard/trainers/_edit_form.html", {
            "trainer": trainer, "error": "Имя и фамилия обязательны",
        })

    if phone and not PHONE_RE.match(phone):
        return render(request, "dashboard/trainers/_edit_form.html", {
            "trainer": trainer, "error": "Некорректный номер телефона",
        })

    update_trainer(
        trainer_id=trainer_id,
        club_id=request.club.id,
        first_name=first_name,
        last_name=last_name,
        phone=phone,
        is_active=is_active,
    )
    trainer = get_trainer_by_id(club=request.club, trainer_id=trainer_id)

    response = render(request, "dashboard/trainers/_edit_form.html", {
        "trainer": trainer, "success": "Сохранено",
    })
    response["HX-Trigger"] = "trainerUpdated"
    return response


def _kind_default(kind: str) -> Decimal:
    from apps.billing.models import TrainingType

    return {
        TrainingType.Kind.GROUP: Decimal("20"),
        TrainingType.Kind.PERSONAL: Decimal("50"),
        TrainingType.Kind.MINI_GROUP: Decimal("40"),
    }.get(kind, Decimal("25"))


def _format_percent(value: Decimal) -> str:
    """Force dot-decimal without locale (for <input type=number> compatibility).
    Strips trailing zeros after the decimal point; integer values stay intact."""
    s = format(value, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s if s else "0"


def _build_rates_grid(locations, training_types, trainer) -> tuple[list[dict], set[int]]:
    """Build [{"location", "checked", "rates": [{"training_type", "percent"}]}, ...]."""
    from apps.trainers.models import TrainerLocation

    current_tl_ids = set(
        TrainerLocation.objects.for_club(trainer.club)
        .filter(trainer=trainer)
        .values_list("location_id", flat=True)
    )
    grid = []
    for loc in locations:
        type_rows = []
        for tt in training_types:
            percent = resolve_trainer_rate(
                club_id=trainer.club_id,
                trainer_id=trainer.id,
                location_id=loc.id,
                training_type_id=tt.id,
            )
            if percent is None:
                percent = _kind_default(tt.kind)
            type_rows.append({
                "training_type": tt,
                "percent": _format_percent(Decimal(str(percent))),
            })
        grid.append({
            "location": loc,
            "checked": loc.id in current_tl_ids,
            "rates": type_rows,
        })
    return grid, current_tl_ids


@management_view_required
def trainer_rates(request: HttpRequest, trainer_id: int) -> HttpResponse:
    from apps.billing.models import TrainingType
    from apps.trainers.models import Trainer, TrainerLocation

    try:
        trainer = get_trainer_by_id(club=request.club, trainer_id=trainer_id)
    except Trainer.DoesNotExist:
        raise Http404

    locations = list(Location.objects.filter(club=request.club))
    # Only active types: inactive TrainingTypes are hidden from the rates
    # form so disabled disciplines don't clutter the UI. Existing TrainerRate
    # rows for inactive types remain in DB (ready to re-appear if the type
    # is reactivated).
    training_types = list(
        TrainingType.objects.for_club(request.club.id).filter(is_active=True)
    )

    def _render(extra=None):
        grid, current_location_ids = _build_rates_grid(locations, training_types, trainer)
        ctx = {
            "trainer": trainer,
            "rates_grid": grid,
            "current_location_ids": current_location_ids,
        }
        if extra:
            ctx.update(extra)
        return render(request, "dashboard/trainers/_rates_form.html", ctx)

    if request.method == "GET":
        return _render()

    # POST: partial-update semantics.
    # - checkbox unchecked → delete TrainerLocation + cascade-delete its TrainerRate
    # - checkbox checked → ensure TrainerLocation exists
    # - non-empty percent cell → validate and upsert TrainerRate (0..100)
    # - empty percent cell → skip (keep existing TrainerRate as-is)
    # - invalid non-empty cell → render form with error, don't save anything
    selected_loc_ids = {
        loc.id for loc in locations if request.POST.get(f"location_{loc.id}")
    }
    if not selected_loc_ids:
        return _render({"error": "Выберите хотя бы одну локацию"})

    from django.db import transaction as _tx

    from apps.trainers.models import TrainerRate as _TrainerRate
    from apps.trainers.services import update_trainer_rates as _update_rates

    # Parse all cells first (fail-fast on invalid values without mutating DB).
    partial_rates: list[dict] = []
    try:
        for loc in locations:
            if loc.id not in selected_loc_ids:
                continue
            for tt in training_types:
                raw = request.POST.get(f"percent_{loc.id}_{tt.id}")
                value = _parse_rate_or_none(raw)
                if value is None:
                    continue  # empty → keep existing, don't touch
                partial_rates.append({
                    "location_id": loc.id,
                    "training_type_id": tt.id,
                    "percent": value,
                })
    except ValueError as e:
        return _render({"error": str(e)})

    try:
        with _tx.atomic():
            current_tl_loc_ids = set(
                TrainerLocation.objects.for_club(request.club)
                .filter(trainer_id=trainer_id)
                .values_list("location_id", flat=True)
            )
            removed = current_tl_loc_ids - selected_loc_ids
            added = selected_loc_ids - current_tl_loc_ids

            if removed:
                _TrainerRate.objects.for_club(request.club).filter(
                    trainer_id=trainer_id, location_id__in=removed,
                ).delete()
                TrainerLocation.objects.for_club(request.club).filter(
                    trainer_id=trainer_id, location_id__in=removed,
                ).delete()

            for loc_id in added:
                TrainerLocation.objects.get_or_create(
                    club=request.club,
                    trainer_id=trainer_id,
                    location_id=loc_id,
                )

            if partial_rates:
                _update_rates(
                    club_id=request.club.id,
                    trainer_id=trainer_id,
                    rates=partial_rates,
                )
    except BusinessLogicError as e:
        return _render({"error": str(e)})

    trainer = get_trainer_by_id(club=request.club, trainer_id=trainer_id)
    response = _render({"success": "Ставки сохранены"})
    response["HX-Trigger"] = "trainerUpdated"
    return response


@management_view_required
def trainer_earning_correction(request: HttpRequest, earning_id: int) -> HttpResponse:
    earning = _get_earning_for_correction(club=request.club, earning_id=earning_id)
    if earning is None:
        raise Http404

    target_trainers = list(
        get_trainers(club=request.club)
        .exclude(id=earning.trainer_id)
        .order_by("first_name", "last_name")
    )
    idempotency_key = request.POST.get("idempotency_key") or f"htmx-earning-correction-{uuid.uuid4()}"

    def _render(extra=None, *, status: int = 200):
        context = {
            "earning": earning,
            "target_trainers": target_trainers,
            "idempotency_key": idempotency_key,
        }
        if extra:
            context.update(extra)
        return render(
            request,
            "dashboard/trainers/_earning_correction_form.html",
            context,
            status=status,
        )

    if request.method == "GET":
        return _render()

    try:
        target_trainer_id = int(request.POST.get("target_trainer_id") or "0")
    except ValueError:
        return _render({"error": "Выберите тренера"}, status=400)
    reason = (request.POST.get("reason") or "").strip()
    form_data = {"target_trainer_id": target_trainer_id, "reason": reason}

    try:
        result = correct_trainer_earning(
            club_id=request.club.id,
            earning_id=earning.id,
            target_trainer_id=target_trainer_id,
            reason=reason,
            actor_user_id=request.user.id,
            idempotency_key=idempotency_key,
        )
    except BusinessLogicError as exc:
        return _render({"error": exc.message, "form_data": form_data}, status=400)

    response = _render({
        "success": (
            "Корректировка сохранена"
            if result.created
            else "Корректировка уже была сохранена"
        ),
        "correction_result": result,
    })
    response["HX-Trigger"] = "trainerUpdated"
    return response


# Keep old view as alias for backward compat
trainer_salary = trainer_detail


@management_view_required
def session_checkins(request: HttpRequest) -> HttpResponse:
    """HTMX partial: list of checkins for a specific schedule+date, with cancel buttons."""
    from apps.attendance.models import Checkin

    schedule_id = request.GET.get("schedule_id", "")
    date_str = request.GET.get("date", "")
    try:
        session_date = date.fromisoformat(date_str)
        sid = int(schedule_id)
    except (ValueError, TypeError):
        raise Http404

    checkins = list(
        Checkin.objects.for_club(request.club)
        .filter(schedule_id=sid, date=session_date, deleted_at__isnull=True)
        .select_related("student")
        .order_by("student__last_name", "student__first_name")
    )

    context = {
        "checkins": checkins,
        "schedule_id": sid,
        "session_date": session_date,
    }
    return render(request, "dashboard/trainers/_session_checkins.html", context)
