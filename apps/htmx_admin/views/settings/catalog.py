import json
import logging

from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from apps.clubs.models import Location
from apps.clubs.services import create_location, delete_location, update_location
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required
from apps.grades.models import Grade, GradeSystem
from apps.grades.services import (
    add_grade,
    create_grade_system,
    delete_grade,
    delete_grade_system,
    update_grade,
)

from ._helpers import _parse_int, _settings_context

logger = logging.getLogger(__name__)


@management_view_required
def settings_catalog_view(request: HttpRequest) -> HttpResponse:
    """Catalog tab: locations + grades."""
    ctx = _settings_context("catalog", request)
    ctx["locations"] = Location.objects.filter(club=request.club)
    ctx["grade_systems"] = (
        GradeSystem.objects.for_club(request.club).prefetch_related("grades")
    )
    template = "dashboard/settings/catalog.html"
    if request.htmx:
        return render(request, f"{template}#content", ctx)
    return render(request, template, ctx)


# ── Catalog CRUD: Locations ──────────────────────────────────────────────


@management_view_required
def location_form(request: HttpRequest, location_id: int | None = None) -> HttpResponse:
    """GET: slide-over form. POST: create/update location."""
    club = request.club
    location = None
    error = None

    if location_id:
        location = Location.objects.filter(club=club, id=location_id).first()
        if not location:
            return HttpResponse("Локация не найдена", status=404)

    if request.method == "GET":
        context = {"location": location}
        return render(request, "dashboard/settings/catalog/_location_form.html", context)

    # POST
    name = request.POST.get("name", "").strip()
    address = request.POST.get("address", "").strip()

    if not name:
        error = "Название обязательно"
        context = {"location": location, "error": error}
        return render(request, "dashboard/settings/catalog/_location_form.html", context)

    try:
        if location:
            update_location(location_id=location.id, club_id=club.id, name=name, address=address)
        else:
            create_location(club_id=club.id, name=name, address=address)
    except BusinessLogicError as e:
        context = {"location": location, "error": str(e)}
        return render(request, "dashboard/settings/catalog/_location_form.html", context)

    return HttpResponse(status=204, headers={"HX-Redirect": "/dashboard/settings/catalog/"})


@management_view_required
@require_POST
def location_delete(request: HttpRequest, location_id: int) -> HttpResponse:
    """Delete location (with FK protection check)."""
    try:
        delete_location(location_id=location_id, club_id=request.club.id)
    except BusinessLogicError as e:
        # Return error as HX-Trigger so the page can show a toast/alert
        return HttpResponse(
            str(e), status=422,
            headers={"HX-Reswap": "none", "HX-Trigger": json.dumps({"showError": str(e)})},
        )
    return HttpResponse(status=204, headers={"HX-Redirect": "/dashboard/settings/catalog/"})


# ── Catalog CRUD: Grade Systems ──────────────────────────────────────────

@management_view_required
def grade_system_form(request: HttpRequest) -> HttpResponse:
    """GET: slide-over form for grade system. POST: create."""
    club = request.club
    error = None

    if request.method == "GET":
        return render(request, "dashboard/settings/catalog/_grade_system_form.html", {})

    # POST
    discipline = request.POST.get("discipline", "").strip()
    if not discipline:
        error = "Название дисциплины обязательно"
        return render(request, "dashboard/settings/catalog/_grade_system_form.html", {"error": error})

    try:
        create_grade_system(club_id=club.id, discipline=discipline)
    except BusinessLogicError as e:
        return render(request, "dashboard/settings/catalog/_grade_system_form.html", {"error": str(e)})

    return HttpResponse(status=204, headers={"HX-Redirect": "/dashboard/settings/catalog/"})


@management_view_required
@require_POST
def grade_system_delete(request: HttpRequest, grade_system_id: int) -> HttpResponse:
    """Delete grade system (with student check)."""
    try:
        delete_grade_system(grade_system_id=grade_system_id, club_id=request.club.id)
    except BusinessLogicError as e:
        return HttpResponse(
            str(e), status=422,
            headers={"HX-Reswap": "none", "HX-Trigger": json.dumps({"showError": str(e)})},
        )
    return HttpResponse(status=204, headers={"HX-Redirect": "/dashboard/settings/catalog/"})


# ── Catalog CRUD: Grades ─────────────────────────────────────────────────

@management_view_required
def grade_form(request: HttpRequest, grade_system_id: int, grade_id: int | None = None) -> HttpResponse:
    """GET: slide-over form for grade. POST: create/update."""
    club = request.club
    grade = None
    error = None

    gs = GradeSystem.objects.for_club(club).filter(id=grade_system_id).first()
    if not gs:
        return HttpResponse("Система аттестации не найдена", status=404)

    if grade_id:
        grade = Grade.objects.for_club(club).filter(id=grade_id, grade_system=gs).first()
        if not grade:
            return HttpResponse("Грейд не найден", status=404)

    if request.method == "GET":
        context = {"grade_system": gs, "grade": grade}
        return render(request, "dashboard/settings/catalog/_grade_form.html", context)

    # POST
    name = request.POST.get("name", "").strip()
    order_str = request.POST.get("order", "").strip()
    min_trainings_str = request.POST.get("min_trainings", "0").strip()

    if not name:
        error = "Название обязательно"
    elif not order_str:
        error = "Порядковый номер обязателен"

    order = _parse_int(order_str or "0", min_val=0, max_val=999)
    min_trainings = _parse_int(min_trainings_str or "0", min_val=0, max_val=9999) or 0

    if order is None and not error:
        error = "Некорректный порядковый номер"

    if error:
        context = {"grade_system": gs, "grade": grade, "error": error}
        return render(request, "dashboard/settings/catalog/_grade_form.html", context)

    try:
        if grade:
            update_grade(grade_id=grade.id, club_id=club.id, name=name, order=order, min_trainings=min_trainings)
        else:
            add_grade(club_id=club.id, grade_system_id=gs.id, name=name, order=order, min_trainings=min_trainings)
    except BusinessLogicError as e:
        context = {"grade_system": gs, "grade": grade, "error": str(e)}
        return render(request, "dashboard/settings/catalog/_grade_form.html", context)
    except Exception:
        logger.exception("grade_form_unexpected_error", extra={"club_id": club.id})
        context = {"grade_system": gs, "grade": grade, "error": "Произошла ошибка. Попробуйте снова."}
        return render(request, "dashboard/settings/catalog/_grade_form.html", context)

    return HttpResponse(status=204, headers={"HX-Redirect": "/dashboard/settings/catalog/"})


@management_view_required
@require_POST
def grade_delete(request: HttpRequest, grade_system_id: int, grade_id: int) -> HttpResponse:
    """Delete grade (with student check)."""
    try:
        delete_grade(grade_id=grade_id, club_id=request.club.id)
    except BusinessLogicError as e:
        return HttpResponse(
            str(e), status=422,
            headers={"HX-Reswap": "none", "HX-Trigger": json.dumps({"showError": str(e)})},
        )
    return HttpResponse(status=204, headers={"HX-Redirect": "/dashboard/settings/catalog/"})
