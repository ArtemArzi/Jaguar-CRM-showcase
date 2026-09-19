from __future__ import annotations

import json
import logging
import re
from uuid import uuid4

from django.http import HttpRequest, HttpResponse, HttpResponseBadRequest, HttpResponseNotAllowed
from django.shortcuts import redirect, render

from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required
from apps.onboarding.models import OnboardingDraft
from apps.onboarding.services import (
    finish_onboarding,
    next_onboarding_step,
    save_step,
    skip_step,
    start_onboarding,
)

logger = logging.getLogger(__name__)

ONBOARDING_STEPS = [
    (1, "grades", "Grades & Disciplines"),
    (2, "trainers", "Trainers"),
    (3, "schedule", "Schedule"),
    (4, "students", "Students"),
    (5, "tariffs", "Tariffs"),
]

STEP_TEMPLATE_MAP = {
    1: "dashboard/onboarding/_step_grades.html",
    2: "dashboard/onboarding/_step_trainers.html",
    3: "dashboard/onboarding/_step_schedule.html",
    4: "dashboard/onboarding/_step_students.html",
    5: "dashboard/onboarding/_step_tariffs.html",
}

_INDEXED_FORM_FIELD_RE = re.compile(r"^(?P<list_key>\w+)\[(?P<index>\d+)\](?:\.|\[)(?P<field>\w+)\]?$")
_STEP_LIST_KEY = {
    2: "trainers",
    3: "schedules",
    4: "students",
    5: "tariffs",
}


def _is_truthy_checkbox(value: str | None) -> bool:
    return value in {"1", "true", "True", "on", "yes"}


def _normalize_indexed_value(*, list_key: str, field: str, value: str):
    if list_key == "tariffs" and field == "training_limit" and value == "":
        return None
    return value


def _indexed_row_is_blank(*, list_key: str, row: dict) -> bool:
    if list_key == "schedules":
        meaningful_fields = ("group", "trainer_choice", "legacy_trainer_name")
    elif list_key == "tariffs":
        meaningful_fields = ("name", "price", "training_limit")
    else:
        meaningful_fields = ("first_name", "last_name", "phone")
    return all(row.get(field) in (None, "") for field in meaningful_fields)


def _normalize_onboarding_post_data(post_data, *, step: int) -> dict:
    if step == OnboardingDraft.Step.GRADES:
        return {
            "disciplines": [value for value in post_data.getlist("disciplines") if value],
            "use_templates": _is_truthy_checkbox(post_data.get("use_templates")),
        }

    list_key = _STEP_LIST_KEY.get(step)
    if list_key is None:
        return {k: v for k, v in post_data.dict().items() if k != "csrfmiddlewaretoken"}

    rows_by_index: dict[int, dict] = {}
    for key in post_data.keys():
        if key == "csrfmiddlewaretoken":
            continue
        match = _INDEXED_FORM_FIELD_RE.match(key)
        if match is None or match.group("list_key") != list_key:
            continue
        index = int(match.group("index"))
        field = match.group("field")
        value = post_data.get(key, "")
        rows_by_index.setdefault(index, {})[field] = _normalize_indexed_value(
            list_key=list_key,
            field=field,
            value=value,
        )

    rows = [
        row
        for _, row in sorted(rows_by_index.items())
        if not _indexed_row_is_blank(list_key=list_key, row=row)
    ]
    if list_key == "trainers":
        for row in rows:
            row["client_ref"] = row.get("client_ref") or str(uuid4())
    if list_key == "schedules":
        rows = [_normalize_schedule_row(row) for row in rows]
    return {list_key: rows}


def _normalize_schedule_row(row: dict) -> dict:
    trainer_choice = row.pop("trainer_choice", "")
    row["trainer_id"] = None
    row["trainer_ref"] = None
    if trainer_choice.startswith("existing:"):
        try:
            row["trainer_id"] = int(trainer_choice.removeprefix("existing:"))
        except ValueError:
            pass
    elif trainer_choice.startswith("draft:"):
        row["trainer_ref"] = trainer_choice.removeprefix("draft:")

    try:
        row["location_id"] = int(row["location_id"])
    except (KeyError, TypeError, ValueError):
        row["location_id"] = None
    return row


def _wizard_context(draft: OnboardingDraft, step: int) -> dict:
    from apps.clubs.models import Location
    from apps.trainers.models import Trainer

    steps_info = [
        {"number": number, "slug": slug, "label": label, "done": number < step, "current": number == step}
        for number, slug, label in ONBOARDING_STEPS
    ]
    step_data = draft.data.get(str(step), {})
    trainer_choices = [
        {
            "value": f"existing:{trainer.id}",
            "label": f"{trainer.first_name} {trainer.last_name}".strip() + f" · в клубе #{trainer.id}",
            "kind": "existing",
        }
        for trainer in Trainer.objects.for_club(draft.club_id).filter(is_active=True).order_by("id")
    ]
    for trainer in draft.data.get(str(OnboardingDraft.Step.TRAINERS), {}).get("trainers", []):
        trainer_choices.append(
            {
                "value": f"draft:{trainer['client_ref']}",
                "label": (
                    f"{trainer.get('first_name', '')} {trainer.get('last_name', '')}".strip()
                    + f" · новый {trainer['client_ref'][:8]}"
                ),
                "kind": "draft",
            }
        )

    rows = _rows_for_step(step=step, step_data=step_data)
    return {
        "draft": draft,
        "current_step": step,
        "total_steps": len(ONBOARDING_STEPS),
        "progress_pct": step * 20,
        "steps": steps_info,
        "step_template": STEP_TEMPLATE_MAP.get(step, STEP_TEMPLATE_MAP[1]),
        "step_data": step_data,
        "rows_json": json.dumps(rows, ensure_ascii=False),
        "trainer_choices": trainer_choices,
        "locations": Location.objects.filter(club_id=draft.club_id).order_by("id"),
    }


def _rows_for_step(*, step: int, step_data: dict) -> list[dict]:
    if step == OnboardingDraft.Step.TRAINERS:
        return step_data.get("trainers") or [
            {"client_ref": str(uuid4()), "first_name": "", "last_name": "", "phone": ""}
        ]
    if step == OnboardingDraft.Step.SCHEDULE:
        rows = []
        for item in step_data.get("schedules", []):
            row = dict(item)
            if row.get("trainer_id"):
                row["trainer_choice"] = f"existing:{row['trainer_id']}"
            elif row.get("trainer_ref"):
                row["trainer_choice"] = f"draft:{row['trainer_ref']}"
            else:
                row["trainer_choice"] = ""
            rows.append(row)
        return rows or [
            {
                "day": "0",
                "start": "10:00",
                "end": "11:30",
                "group": "",
                "trainer_choice": "",
                "location_id": "",
                "legacy_trainer_name": "",
            }
        ]
    if step == OnboardingDraft.Step.STUDENTS:
        return step_data.get("students") or [{"first_name": "", "last_name": "", "phone": ""}]
    if step == OnboardingDraft.Step.TARIFFS:
        return step_data.get("tariffs") or [
            {"name": "", "price": "", "training_limit": "", "duration_days": "30"}
        ]
    return []


def _redirect_response(request: HttpRequest, url: str) -> HttpResponse:
    if request.headers.get("HX-Request") == "true":
        return HttpResponse(status=204, headers={"HX-Redirect": url})
    return redirect(url)


def _finish_error_response(
    request: HttpRequest,
    *,
    draft: OnboardingDraft,
    error: BusinessLogicError,
) -> HttpResponse:
    error_step = (
        OnboardingDraft.Step.SCHEDULE
        if error.code.startswith("onboarding_schedule_") or error.code == "invalid_onboarding_schedule_time"
        else draft.current_step
    )
    draft.refresh_from_db()
    context = _wizard_context(draft, error_step)
    context["error"] = error.message
    context["error_code"] = error.code
    return render(request, STEP_TEMPLATE_MAP[error_step], context)


def _finish_draft(request: HttpRequest, *, draft: OnboardingDraft) -> HttpResponse:
    try:
        finish_onboarding(club_id=request.club.id, draft_id=draft.id)
    except BusinessLogicError as exc:
        logger.warning(
            "onboarding_finish_failed",
            extra={"club_id": request.club.id, "draft_id": draft.id, "error_code": exc.code},
        )
        return _finish_error_response(request, draft=draft, error=exc)
    return _redirect_response(request, "/dashboard/")


@management_view_required
def onboarding_wizard(request: HttpRequest) -> HttpResponse:
    draft = start_onboarding(club_id=request.club.id)
    context = _wizard_context(draft, draft.current_step)
    return render(request, "dashboard/onboarding/wizard.html", context)


@management_view_required
def onboarding_step(request: HttpRequest, step: int) -> HttpResponse:
    if step not in {number for number, _, _ in ONBOARDING_STEPS}:
        return redirect("/dashboard/onboarding/")

    draft = start_onboarding(club_id=request.club.id)
    if request.method == "POST":
        data = _normalize_onboarding_post_data(request.POST, step=step)
        try:
            draft = save_step(
                club_id=request.club.id,
                draft_id=draft.id,
                step=step,
                data=data,
            )
        except BusinessLogicError as exc:
            context = _wizard_context(draft, step)
            context["error"] = exc.message
            context["error_code"] = exc.code
            return render(request, STEP_TEMPLATE_MAP[step], context)

        next_step = next_onboarding_step(step)
        if next_step is None:
            return _finish_draft(request, draft=draft)
        context = _wizard_context(draft, next_step)
        return render(request, STEP_TEMPLATE_MAP[next_step], context)

    context = _wizard_context(draft, step)
    return render(request, STEP_TEMPLATE_MAP[step], context)


@management_view_required
def onboarding_skip(request: HttpRequest, step: int) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if step not in {number for number, _, _ in ONBOARDING_STEPS}:
        return redirect("/dashboard/onboarding/")

    draft = start_onboarding(club_id=request.club.id)
    draft = skip_step(club_id=request.club.id, draft_id=draft.id, step=step)
    next_step = next_onboarding_step(step)
    if next_step is None:
        return _finish_draft(request, draft=draft)
    context = _wizard_context(draft, next_step)
    return render(request, STEP_TEMPLATE_MAP[next_step], context)


@management_view_required
def onboarding_finish(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    try:
        draft_id = int(request.POST.get("draft_id", ""))
    except ValueError:
        return HttpResponseBadRequest("draft_id is required")

    draft = OnboardingDraft.objects.for_club(request.club).filter(id=draft_id).first()
    if draft is None:
        return HttpResponseBadRequest("draft_id is invalid")
    return _finish_draft(request, draft=draft)
