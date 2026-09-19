import logging

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render

from apps.billing.services import update_club_settings
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required

from ._helpers import _HEX_COLOR_RE, _VALID_TIMEZONES, _parse_int, _settings_context

logger = logging.getLogger(__name__)


@management_view_required
def settings_redirect(request: HttpRequest) -> HttpResponse:
    return redirect("settings-general")


@management_view_required
def settings_general_view(request: HttpRequest) -> HttpResponse:
    ctx = _settings_context("general", request)
    error = None

    if request.method == "POST":
        fields: dict = {}

        # --- Branding ---
        club_name_display = request.POST.get("club_name_display", "").strip()
        primary_color = request.POST.get("primary_color", "").strip()

        if club_name_display:
            fields["club_name_display"] = club_name_display

        if primary_color and _HEX_COLOR_RE.match(primary_color):
            fields["primary_color"] = primary_color
            fields["accent_color"] = primary_color  # accent = primary for consistency

        # --- Logo file upload (not via service — ImageField handled directly) ---
        logo_file = request.FILES.get("logo_file")
        logo_remove = request.POST.get("logo_remove") == "1"
        logo_error: str | None = None
        settings_instance = ctx["settings"]  # reuse instance from _settings_context
        if logo_file:
            # Validate size (max 2 MB)
            if logo_file.size > 2 * 1024 * 1024:
                logo_error = "Файл слишком большой. Максимум 2 МБ."
            # Whitelist of accepted raster formats. SVG is rejected — see
            # security review: hand-rolled SVG sanitizer is unsafe and adding
            # a real one (defusedxml + allowlist) is out of scope for tier 1.
            elif logo_file.content_type not in ("image/png", "image/jpeg", "image/webp"):
                logo_error = "Файл должен быть PNG, JPG или WEBP."
            else:
                try:
                    from PIL import Image, UnidentifiedImageError
                    img = Image.open(logo_file)
                    img.verify()
                    logo_file.seek(0)  # verify() exhausts the stream
                except (UnidentifiedImageError, OSError, ValueError) as e:
                    logger.warning("logo_upload_invalid", extra={"club_id": request.club.id, "error": str(e)})
                    logo_error = "Файл не является корректным изображением."

                if not logo_error:
                    # Delete old file before replacing
                    if settings_instance.logo_file:
                        settings_instance.logo_file.delete(save=False)
                    settings_instance.logo_file = logo_file
                    settings_instance.save(update_fields=["logo_file", "updated_at"])
                    # Clear external URL since local file takes priority
                    fields["logo_url"] = ""
        elif logo_remove and settings_instance.logo_file:
            settings_instance.logo_file.delete(save=False)
            settings_instance.logo_file = None
            settings_instance.save(update_fields=["logo_file", "updated_at"])

        # --- Timezone (on Club model, not ClubSettings) ---
        timezone_val = request.POST.get("timezone", "").strip()
        club_update_fields = []
        if timezone_val and timezone_val in _VALID_TIMEZONES:
            request.club.timezone = timezone_val
            club_update_fields.append("timezone")

        if club_update_fields:
            request.club.save(update_fields=club_update_fields)

        # --- Freeze settings ---
        fields["freeze_enabled"] = request.POST.get("freeze_enabled") == "on"

        freeze_max_days = _parse_int(
            request.POST.get("freeze_max_days", ""), min_val=1, max_val=365,
        )
        if freeze_max_days is not None:
            fields["freeze_max_days"] = freeze_max_days

        freeze_max_count_str = request.POST.get("freeze_max_count", "").strip()
        if freeze_max_count_str:
            freeze_max_count = _parse_int(freeze_max_count_str, min_val=1, max_val=99)
            fields["freeze_max_count"] = freeze_max_count  # None if invalid -> unlimited
        else:
            fields["freeze_max_count"] = None  # empty = unlimited

        min_trainings = _parse_int(
            request.POST.get("min_trainings_to_freeze", ""), min_val=0, max_val=99,
        )
        if min_trainings is not None:
            fields["min_trainings_to_freeze"] = min_trainings

        if logo_error:
            error = logo_error
        else:
            try:
                update_club_settings(club_id=request.club.id, **fields)
            except BusinessLogicError as e:
                logger.error(
                    "club_settings_update_failed",
                    extra={"club_id": request.club.id, "error": str(e)},
                )
                error = str(e)
            else:
                cache.delete(f"club_branding:{request.club.id}")
                logger.info(
                    "club_settings_saved_via_admin",
                    extra={"club_id": request.club.id, "fields": list(fields.keys())},
                )
                # Full page reload so CSS variables in <head> update
                return HttpResponse(
                    status=204,
                    headers={"HX-Refresh": "true"},
                )

    ctx["saved"] = False
    ctx["error"] = error
    template = "dashboard/settings/general.html"
    if request.htmx:
        return render(request, f"{template}#content", ctx)
    return render(request, template, ctx)
