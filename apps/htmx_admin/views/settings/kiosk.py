from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from apps.attendance.models import KioskDevice
from apps.attendance.services import deactivate_kiosk, generate_kiosk_pin
from apps.common.permissions import management_view_required

from ._helpers import _settings_context


@management_view_required
def settings_kiosk_view(request: HttpRequest) -> HttpResponse:
    """Kiosk tab: PIN generation, device status."""
    ctx = _settings_context("kiosk", request)
    ctx["kiosk_device"] = (
        KioskDevice.objects.filter(club=request.club, is_active=True).first()
    )
    template = "dashboard/settings/kiosk.html"
    if request.htmx:
        return render(request, f"{template}#content", ctx)
    return render(request, template, ctx)


@management_view_required
@require_POST
def kiosk_generate_pin(request: HttpRequest) -> HttpResponse:
    pin = generate_kiosk_pin(club_id=request.club.id)
    kiosk_device = KioskDevice.objects.filter(club=request.club, is_active=True).first()
    context = {"kiosk_pin": pin, "kiosk_device": kiosk_device}
    return render(request, "dashboard/settings/_kiosk_status.html", context)


@management_view_required
@require_POST
def kiosk_deactivate(request: HttpRequest) -> HttpResponse:
    deactivate_kiosk(club_id=request.club.id)
    context = {"kiosk_device": None}
    return render(request, "dashboard/settings/_kiosk_status.html", context)
