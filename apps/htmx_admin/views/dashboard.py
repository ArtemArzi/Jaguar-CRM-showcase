import logging
from datetime import date, timedelta

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_not_required
from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from apps.clubs.timezones import club_localdate
from apps.common.middleware import client_ip_hash
from apps.common.permissions import management_view_required
from apps.dashboard.selectors import get_attention_alerts, get_dashboard_metrics
from apps.dashboard.services import get_salary_total

logger = logging.getLogger(__name__)

MAX_LOGIN_ATTEMPTS = 5
LOGIN_BLOCK_SECONDS = 300  # 5 min

PERIOD_CHOICES = [("today", "Сегодня"), ("week", "Неделя"), ("month", "Месяц")]

ALERT_META = {
    "expiring_subscriptions": {
        "label": "Истекающие абонементы",
        "url": "/dashboard/billing/",
        "icon": "\u26a0\ufe0f",
        "border": "border-amber-500",
    },
    "unconfirmed_payments": {
        "label": "Неподтверждённые оплаты",
        "url": "/dashboard/billing/payments/",
        "icon": "\U0001f4b0",
        "border": "border-blue-500",
    },
    "at_risk_students": {
        "label": "Ученики в зоне риска",
        "url": "/dashboard/students/?status=at_risk",
        "icon": "\u26a0\ufe0f",
        "border": "border-red-500",
    },
    "overdue_retention_tasks": {
        "label": "Просроченные задачи удержания",
        "url": "/dashboard/retention/",
        "icon": "\U0001f4cb",
        "border": "border-purple-500",
    },
}


def _resolve_period(period: str, *, today: date) -> date:
    if period == "week":
        return today - timedelta(days=today.weekday())
    if period == "month":
        return today.replace(day=1)
    return today


def _enrich_alerts(raw_alerts: list[dict]) -> list[dict]:
    """Add label, url, icon, border to raw alert dicts from selector."""
    enriched = []
    for alert in raw_alerts:
        meta = ALERT_META.get(alert["type"], {})
        enriched.append({
            **alert,
            "label": meta.get("label", alert["type"]),
            "url": meta.get("url", "/dashboard/"),
            "icon": meta.get("icon", ""),
            "border": meta.get("border", "border-gray-500"),
        })
    return enriched


def _dashboard_context(request: HttpRequest) -> dict:
    from apps.attendance.selectors import get_schedule_occurrences_for_date

    period = request.GET.get("period", "today")
    today = club_localdate(request.club)
    date_from = _resolve_period(period, today=today)
    metrics = get_dashboard_metrics(club=request.club, date_from=date_from, date_to=today)

    salary_total = get_salary_total(club=request.club, date_from=date_from, date_to=today)
    metrics["trainer_salaries"] = salary_total
    if metrics["revenue"]:
        metrics["salary_percent"] = round(salary_total / metrics["revenue"] * 100) if metrics["revenue"] else 0

    raw_alerts = get_attention_alerts(club=request.club)
    alerts = _enrich_alerts(raw_alerts)

    # Today's sessions
    sessions = get_schedule_occurrences_for_date(
        club=request.club,
        target_date=today,
    )
    today_sessions = [
        {
            "time": s.effective_start_time.strftime("%H:%M"),
            "name": s.group_name,
            "trainer": s.trainer_name,
        }
        for s in sessions
    ]

    return {
        "page_title": "Дашборд",
        "metrics": metrics,
        "alerts": alerts,
        "today_sessions": today_sessions,
        "period": period,
        "period_choices": PERIOD_CHOICES,
    }


@management_view_required
def dashboard_home(request: HttpRequest) -> HttpResponse:
    context = _dashboard_context(request)
    if request.htmx:
        return render(request, "dashboard/index.html#content", context)
    return render(request, "dashboard/index.html", context)


@management_view_required
def dashboard_metrics_partial(request: HttpRequest) -> HttpResponse:
    context = _dashboard_context(request)
    return render(request, "dashboard/index.html#metrics_section", context)


@login_not_required
def admin_login(request: HttpRequest) -> HttpResponse:
    if request.method == "POST":
        ip = request.META.get("REMOTE_ADDR", "unknown")
        ip_hash = client_ip_hash(request)
        email = request.POST.get("email", "").strip().lower()
        password = request.POST.get("password", "")

        # Compound throttle: block if EITHER ip-bucket OR email-bucket is full.
        # Stops single-account brute force across rotating IPs.
        ip_key = f"login_attempts:ip:{ip}"
        email_key = f"login_attempts:email:{email}" if email else None
        ip_attempts = cache.get(ip_key, 0)
        email_attempts = cache.get(email_key, 0) if email_key else 0
        if ip_attempts >= MAX_LOGIN_ATTEMPTS or email_attempts >= MAX_LOGIN_ATTEMPTS:
            logger.warning("login_blocked", extra={"client_ip_hash": ip_hash})
            return render(request, "auth/login.html", {"error": "Too many attempts. Try again in 5 minutes."})

        user = authenticate(request, username=email, password=password)
        if user is not None:
            cache.delete(ip_key)
            if email_key:
                cache.delete(email_key)
            login(request, user)
            return redirect("/dashboard/")
        cache.set(ip_key, ip_attempts + 1, LOGIN_BLOCK_SECONDS)
        if email_key:
            cache.set(email_key, email_attempts + 1, LOGIN_BLOCK_SECONDS)
        logger.warning("login_failed", extra={"client_ip_hash": ip_hash})
        return render(request, "auth/login.html", {"error": "Неверный email или пароль"})
    return render(request, "auth/login.html")


def admin_logout(request: HttpRequest) -> HttpResponse:
    logout(request)
    return redirect("/dashboard/login/")
