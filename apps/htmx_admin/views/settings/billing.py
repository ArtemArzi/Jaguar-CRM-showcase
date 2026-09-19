import logging
import uuid
from decimal import Decimal, InvalidOperation

from django.db.models import Prefetch, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from apps.billing.models import Discount, Tariff, TariffComponent, TrainingType
from apps.billing.service_modules.tariff_revisions import revise_tariff_price
from apps.billing.services import (
    create_discount,
    create_tariff,
    create_training_type,
    is_training_type_kind_locked,
    update_discount,
    update_tariff,
    update_training_type,
)
from apps.clubs.models import Location
from apps.common.exceptions import BusinessLogicError
from apps.common.permissions import management_view_required
from apps.grades.models import GradeSystem
from apps.trainers.models import Trainer

from ._helpers import _parse_int, _settings_context

logger = logging.getLogger(__name__)


_COMPONENT_ROW_COUNT = 3
_UNSET = object()

_PAYOUT_POLICY_LABELS = {
    "": "Авто по типу",
    Tariff.PayoutPolicy.ON_PAYMENT: "После оплаты",
    Tariff.PayoutPolicy.ON_CHECKIN: "За посещение",
    Tariff.PayoutPolicy.NONE: "Без выплаты",
}

_ENTITLEMENT_LABELS = {
    TariffComponent.EntitlementKind.FINITE_CREDITS: "Кол-во занятий",
    TariffComponent.EntitlementKind.WEEKLY_LIMIT: "Лимит в неделю",
    TariffComponent.EntitlementKind.UNLIMITED: "Безлимит",
}


def _payout_policy_label(policy: str) -> str:
    return _PAYOUT_POLICY_LABELS.get(policy, policy)


def _default_payout_policy_for_kind(kind: str) -> str:
    if kind == TrainingType.Kind.GROUP:
        return Tariff.PayoutPolicy.ON_PAYMENT
    if kind in {TrainingType.Kind.PERSONAL, TrainingType.Kind.MINI_GROUP}:
        return Tariff.PayoutPolicy.ON_CHECKIN
    return Tariff.PayoutPolicy.NONE


def _resolved_tariff_payout_policy(tariff: Tariff) -> str:
    return tariff.trainer_payout_policy or _default_payout_policy_for_kind(tariff.training_type.kind)


def _component_entitlement_label(component: TariffComponent) -> str:
    if component.entitlement_kind == TariffComponent.EntitlementKind.FINITE_CREDITS:
        return f"{component.credits_total or 0} занятий"
    if component.entitlement_kind == TariffComponent.EntitlementKind.WEEKLY_LIMIT:
        return f"{component.weekly_limit or 0}/нед."
    return "безлимит"


def _blank_component_row(index: int) -> dict:
    return {
        "index": index,
        "name": "",
        "training_type_id": "",
        "entitlement_kind": TariffComponent.EntitlementKind.FINITE_CREDITS,
        "credits_total": "",
        "weekly_limit": "",
        "trainer_payout_policy": "",
        "paid_amount_basis": "",
    }


def _component_row_from_model(component: TariffComponent, index: int) -> dict:
    return {
        "index": index,
        "name": component.name,
        "training_type_id": str(component.training_type_id),
        "entitlement_kind": component.entitlement_kind,
        "credits_total": component.credits_total or "",
        "weekly_limit": component.weekly_limit or "",
        "trainer_payout_policy": component.trainer_payout_policy,
        "paid_amount_basis": component.paid_amount_basis,
    }


def _active_tariff_components(club, tariff: Tariff | None) -> list[TariffComponent]:
    if not tariff:
        return []
    return list(
        TariffComponent.objects.for_club(club)
        .filter(tariff=tariff, is_active=True)
        .select_related("training_type", "location")
        .order_by("sort_order", "id")
    )


def _tariff_has_custom_components(club, tariff: Tariff | None) -> bool:
    components = _active_tariff_components(club, tariff)
    if not tariff or len(components) != 1:
        return bool(components)
    component = components[0]
    return (
        component.training_type_id != tariff.training_type_id
        or component.entitlement_kind
        != (
            TariffComponent.EntitlementKind.FINITE_CREDITS
            if tariff.trainings_limit is not None
            else TariffComponent.EntitlementKind.UNLIMITED
        )
        or component.credits_total != tariff.trainings_limit
        or component.weekly_limit is not None
        or component.scope != tariff.scope
        or component.location_id != tariff.location_id
        or component.trainer_payout_policy != _resolved_tariff_payout_policy(tariff)
        or component.paid_amount_basis != tariff.price
    )


def _tariff_component_form_rows(club, tariff: Tariff | None) -> list[dict]:
    rows = [
        _component_row_from_model(component, index)
        for index, component in enumerate(_active_tariff_components(club, tariff))
    ]
    while len(rows) < _COMPONENT_ROW_COUNT:
        rows.append(_blank_component_row(len(rows)))
    return rows[:_COMPONENT_ROW_COUNT]


def _parse_component_rows(
    request: HttpRequest,
    *,
    scope: str,
    location_id: int | None,
) -> tuple[list[dict], list[dict] | None, str | None]:
    rows: list[dict] = []
    components: list[dict] = []
    entitlement_values = set(TariffComponent.EntitlementKind.values)

    for index in range(_COMPONENT_ROW_COUNT):
        row = _blank_component_row(index)
        row["name"] = request.POST.get(f"component_name_{index}", "").strip()
        row["training_type_id"] = request.POST.get(f"component_training_type_id_{index}", "").strip()
        row["entitlement_kind"] = (
            request.POST.get(f"component_entitlement_kind_{index}", "").strip()
            or TariffComponent.EntitlementKind.FINITE_CREDITS
        )
        row["credits_total"] = request.POST.get(f"component_credits_total_{index}", "").strip()
        row["weekly_limit"] = request.POST.get(f"component_weekly_limit_{index}", "").strip()
        row["trainer_payout_policy"] = request.POST.get(
            f"component_trainer_payout_policy_{index}", ""
        ).strip()
        row["paid_amount_basis"] = request.POST.get(f"component_paid_amount_basis_{index}", "").strip()
        rows.append(row)

        has_row = any(
            [
                row["name"],
                row["training_type_id"],
                (
                    row["entitlement_kind"]
                    if row["entitlement_kind"] != TariffComponent.EntitlementKind.FINITE_CREDITS
                    else ""
                ),
                row["credits_total"],
                row["weekly_limit"],
                row["trainer_payout_policy"],
                row["paid_amount_basis"],
            ]
        )
        if not has_row:
            continue

        prefix = f"Компонент {index + 1}:"
        training_type_id = _parse_int(row["training_type_id"], min_val=1, max_val=999999999)
        if not training_type_id:
            return rows, None, f"{prefix} выберите тип тренировки"

        try:
            paid_amount_basis = Decimal(str(row["paid_amount_basis"]))
            if paid_amount_basis <= 0:
                return rows, None, f"{prefix} база суммы должна быть больше 0"
        except (InvalidOperation, ValueError):
            return rows, None, f"{prefix} некорректная база суммы"

        if row["entitlement_kind"] not in entitlement_values:
            return rows, None, f"{prefix} некорректный лимит"

        credits_total = None
        weekly_limit = None
        if row["entitlement_kind"] == TariffComponent.EntitlementKind.FINITE_CREDITS:
            credits_total = _parse_int(row["credits_total"], min_val=1, max_val=9999)
            if not credits_total:
                return rows, None, f"{prefix} укажите количество занятий"
        elif row["entitlement_kind"] == TariffComponent.EntitlementKind.WEEKLY_LIMIT:
            weekly_limit = _parse_int(row["weekly_limit"], min_val=1, max_val=99)
            if not weekly_limit:
                return rows, None, f"{prefix} укажите лимит в неделю"

        payout_policy = row["trainer_payout_policy"]
        if payout_policy and payout_policy not in Tariff.PayoutPolicy.values:
            return rows, None, f"{prefix} некорректная выплата"

        components.append(
            {
                "name": row["name"],
                "training_type_id": training_type_id,
                "entitlement_kind": row["entitlement_kind"],
                "credits_total": credits_total,
                "weekly_limit": weekly_limit,
                "scope": scope,
                "location_id": location_id,
                "trainer_payout_policy": payout_policy,
                "paid_amount_basis": paid_amount_basis,
            }
        )

    if not components:
        return rows, None, "Добавьте хотя бы один компонент абонемента"
    return rows, components, None


def _default_component_payload(
    *,
    name: str,
    training_type_id: int,
    price: Decimal,
    trainings_limit: int | None,
    trainer_payout_policy: str,
    scope: str,
    location_id: int | None,
) -> list[dict]:
    return [
        {
            "name": name,
            "training_type_id": training_type_id,
            "entitlement_kind": (
                TariffComponent.EntitlementKind.FINITE_CREDITS
                if trainings_limit is not None
                else TariffComponent.EntitlementKind.UNLIMITED
            ),
            "credits_total": trainings_limit,
            "weekly_limit": None,
            "scope": scope,
            "location_id": location_id,
            "trainer_payout_policy": trainer_payout_policy,
            "paid_amount_basis": price,
        }
    ]


def _component_payloads_equal(
    *,
    club,
    tariff: Tariff,
    payloads: list[dict],
) -> bool:
    components = _active_tariff_components(club, tariff)
    if len(components) != len(payloads):
        return False
    for component, payload in zip(components, payloads, strict=True):
        if (
            component.name != payload["name"]
            or component.training_type_id != payload["training_type_id"]
            or component.entitlement_kind != payload["entitlement_kind"]
            or component.credits_total != payload["credits_total"]
            or component.weekly_limit != payload["weekly_limit"]
            or component.scope != payload["scope"]
            or component.location_id != payload["location_id"]
            or component.trainer_payout_policy != payload["trainer_payout_policy"]
            or component.paid_amount_basis != payload["paid_amount_basis"]
        ):
            return False
    return True


def _annotate_tariff_for_admin_list(tariff: Tariff) -> None:
    components = list(getattr(tariff, "active_components", []))
    if not components:
        tariff.component_summary = ""
        tariff.payout_summary = _payout_policy_label(tariff.trainer_payout_policy)
        return

    if len(components) == 1:
        component = components[0]
        tariff.component_summary = ""
        tariff.payout_summary = _payout_policy_label(component.trainer_payout_policy)
        return

    tariff.component_summary = " · ".join(
        (
            f"{component.name or component.training_type.name}: "
            f"{_component_entitlement_label(component)}, {component.paid_amount_basis} ₽"
        )
        for component in components
    )
    labels = []
    for component in components:
        label = _payout_policy_label(component.trainer_payout_policy)
        if label not in labels:
            labels.append(label)
    tariff.payout_summary = " / ".join(labels)


def _render_billing_tab(
    request: HttpRequest, *, saved: bool = False, error: str | None = None,
) -> HttpResponse:
    """Read-only render of billing tab. Used by GET and by toggle endpoints."""
    ctx = _settings_context("billing", request)
    ctx["training_types"] = list(
        TrainingType.objects.for_club(request.club)
        .select_related("grade_system")
        .order_by("-is_active", "name")
    )
    ctx["grade_systems"] = list(
        GradeSystem.objects.for_club(request.club).filter(is_active=True).order_by("discipline")
    )
    component_qs = (
        TariffComponent.objects.for_club(request.club)
        .filter(is_active=True)
        .select_related("training_type", "location")
        .order_by("sort_order", "id")
    )
    tariffs = list(
        Tariff.objects.for_club(request.club)
        .filter(is_active=True)
        .select_related("training_type", "location", "personal_booking_trainer")
        .prefetch_related(Prefetch("components", queryset=component_qs, to_attr="active_components"))
        .order_by("-is_active", "name")
    )
    for tariff in tariffs:
        _annotate_tariff_for_admin_list(tariff)
    ctx["tariffs"] = tariffs
    ctx["archive_url"] = "/dashboard/settings/billing/tariffs/archive/"
    ctx["discounts"] = list(
        Discount.objects.for_club(request.club).order_by("-is_active", "name")
    )
    ctx["saved"] = saved
    ctx["error"] = error
    template = "dashboard/settings/billing.html"
    if request.htmx:
        return render(request, f"{template}#content", ctx)
    return render(request, template, ctx)


@management_view_required
def settings_billing_view(request: HttpRequest) -> HttpResponse:
    """Billing tab: tariffs, discounts, drop-in pricing."""
    saved = False
    error = None

    if request.method == "POST":
        training_types = list(
            TrainingType.objects.for_club(request.club)
            .select_related("grade_system")
            .order_by("-is_active", "name")
        )
        try:
            for tt in training_types:
                price_str = request.POST.get(f"tt_drop_in_price_{tt.id}", "").strip()
                trial_free_val = request.POST.get(f"tt_trial_free_{tt.id}")
                try:
                    price = Decimal(price_str) if price_str else None
                except InvalidOperation as exc:
                    raise BusinessLogicError("Некорректная цена разового занятия") from exc
                if price is not None and price < 0:
                    raise BusinessLogicError("Цена разового занятия не может быть отрицательной")
                update_training_type(
                    training_type_id=tt.id,
                    club_id=request.club.id,
                    drop_in_price=price,
                    trial_free=trial_free_val == "on",
                )
        except BusinessLogicError as e:
            error = str(e)
        else:
            saved = True

    return _render_billing_tab(request, saved=saved, error=error)


# ── Billing CRUD: Training Types ───────────────────────────────────────


def _training_type_form_ctx(club, tt=None, error: str | None = None) -> dict:
    return {
        "tt": tt,
        "kind_locked": bool(
            tt and is_training_type_kind_locked(club_id=club.id, training_type_id=tt.id)
        ),
        "training_type_kinds": TrainingType.Kind.choices,
        "grade_systems": list(
            GradeSystem.objects.for_club(club).filter(is_active=True).order_by("discipline")
        ),
        "error": error,
    }


@management_view_required
def training_type_form(request: HttpRequest, training_type_id: int | None = None) -> HttpResponse:
    """GET: slide-over form. POST: create/update training type."""
    club = request.club
    tt = None
    if training_type_id:
        tt = TrainingType.objects.for_club(club).filter(id=training_type_id).first()
        if not tt:
            raise Http404

    if request.method == "GET":
        return render(
            request,
            "dashboard/settings/billing/_training_type_form.html",
            _training_type_form_ctx(club, tt),
        )

    name = request.POST.get("name", "").strip()
    if not name:
        return render(
            request,
            "dashboard/settings/billing/_training_type_form.html",
            _training_type_form_ctx(club, tt, "Укажите название"),
        )

    grade_system_id = _parse_int(
        request.POST.get("grade_system_id", ""),
        min_val=1,
        max_val=2_147_483_647,
    )
    kind_raw = request.POST.get("kind")
    if kind_raw is None and tt:
        kind = tt.kind
    else:
        kind = (kind_raw or TrainingType.Kind.GROUP).strip() or TrainingType.Kind.GROUP

    try:
        if tt:
            update_training_type(
                training_type_id=tt.id,
                club_id=club.id,
                name=name,
                kind=kind,
                grade_system_id=grade_system_id,
            )
        else:
            from django.utils.text import slugify
            slug = slugify(name, allow_unicode=True) or name.lower().replace(" ", "-")
            create_training_type(
                club_id=club.id,
                name=name,
                slug=slug,
                kind=kind,
                grade_system_id=grade_system_id,
            )
    except BusinessLogicError as e:
        return render(
            request,
            "dashboard/settings/billing/_training_type_form.html",
            _training_type_form_ctx(club, tt, str(e)),
        )

    return HttpResponse(status=204, headers={"HX-Redirect": "/dashboard/settings/billing/"})


@management_view_required
@require_POST
def training_type_toggle(request: HttpRequest, training_type_id: int) -> HttpResponse:
    """Toggle training type is_active."""
    tt = TrainingType.objects.for_club(request.club).filter(id=training_type_id).first()
    if not tt:
        raise Http404
    update_training_type(training_type_id=tt.id, club_id=request.club.id, is_active=not tt.is_active)
    return _render_billing_tab(request)


# ── Billing CRUD: Tariffs ──────────────────────────────────────────────


def _tariff_form_ctx(
    club,
    tariff=None,
    error: str | None = None,
    *,
    component_rows: list[dict] | None = None,
    use_components: bool | None = None,
    personal_default_checked: bool | None = None,
    personal_booking_trainer_id: int | None | object = _UNSET,
) -> dict:
    """Shared context for tariff form render (GET, validation error, service error)."""
    if component_rows is None:
        component_rows = _tariff_component_form_rows(club, tariff)
    if use_components is None:
        use_components = _tariff_has_custom_components(club, tariff)
    if personal_default_checked is None:
        personal_default_checked = bool(
            tariff and tariff.is_personal_booking_default
        )
    if personal_booking_trainer_id is _UNSET:
        personal_booking_trainer_id = (
            tariff.personal_booking_trainer_id if tariff is not None else None
        )
    return {
        "tariff": tariff,
        "training_types": list(
            TrainingType.objects.for_club(club).filter(is_active=True)
        ),
        "locations": list(Location.objects.filter(club=club)),
        "personal_booking_trainers": list(
            Trainer.objects.for_club(club)
            .filter(
                Q(is_active=True)
                | Q(id=personal_booking_trainer_id)
            )
            .order_by("first_name", "last_name", "id")
        ),
        "payout_policy_options": [
            {"value": value, "label": label}
            for value, label in _PAYOUT_POLICY_LABELS.items()
        ],
        "entitlement_options": [
            {"value": value, "label": label}
            for value, label in _ENTITLEMENT_LABELS.items()
        ],
        "component_rows": component_rows,
        "use_components": use_components,
        "personal_default_checked": personal_default_checked,
        "personal_booking_trainer_id": personal_booking_trainer_id,
        "error": error,
    }


@management_view_required
def tariff_form(request: HttpRequest, tariff_id: int | None = None) -> HttpResponse:
    """GET: show create/edit form in slide-over. POST: save tariff."""
    club = request.club
    tariff = None
    if tariff_id:
        tariff = (
            Tariff.objects.for_club(club)
            .select_related("training_type", "location", "personal_booking_trainer")
            .filter(id=tariff_id)
            .first()
        )
        if not tariff:
            raise Http404

    if request.method == "GET":
        return render(
            request, "dashboard/settings/billing/_tariff_form.html",
            _tariff_form_ctx(club, tariff),
        )

    # POST
    name = request.POST.get("name", "").strip()
    training_type_id_str = request.POST.get("training_type_id", "").strip()
    price_str = request.POST.get("price", "").strip()
    trainings_limit_str = request.POST.get("trainings_limit", "").strip()
    duration_days_str = request.POST.get("duration_days", "").strip()
    description = request.POST.get("description", "").strip()
    scope = request.POST.get("scope", "club").strip()
    location_id_str = request.POST.get("location_id", "").strip()
    trainer_payout_policy = request.POST.get("trainer_payout_policy", "").strip()
    is_personal_booking_default = (
        request.POST.get("is_personal_booking_default") == "on"
    )
    personal_booking_trainer_id_str = request.POST.get(
        "personal_booking_trainer_id", ""
    ).strip()
    personal_booking_trainer_id = (
        _parse_int(
            personal_booking_trainer_id_str,
            min_val=1,
            max_val=999999999,
        )
        if personal_booking_trainer_id_str
        else None
    )
    use_components = request.POST.get("use_components") == "on"
    component_rows: list[dict] | None = None
    component_payloads: list[dict] | None = None

    error = None
    if not name:
        error = "Укажите название тарифа"

    training_type_id = _parse_int(
        training_type_id_str, min_val=1, max_val=999999999
    )
    if not error and not training_type_id:
        error = "Выберите тип тренировки"

    price = None
    if not error:
        try:
            price = Decimal(price_str)
            if price <= 0:
                error = "Цена должна быть больше 0"
        except (InvalidOperation, ValueError):
            error = "Некорректная цена"

    duration_days = _parse_int(duration_days_str, min_val=1, max_val=365)
    if not error and not duration_days:
        error = "Укажите срок действия (1-365 дней)"

    trainings_limit = (
        _parse_int(trainings_limit_str, min_val=1, max_val=9999)
        if trainings_limit_str
        else None
    )

    location_id = (
        _parse_int(location_id_str, min_val=1, max_val=999999999)
        if location_id_str
        else None
    )
    if scope == "location" and not location_id:
        error = error or "Выберите зал для тарифа с привязкой к залу"

    if not error and trainer_payout_policy and trainer_payout_policy not in Tariff.PayoutPolicy.values:
        error = "Некорректная выплата тренеру"

    if not error and use_components:
        component_rows, component_payloads, component_error = _parse_component_rows(
            request,
            scope=scope,
            location_id=location_id,
        )
        error = component_error

    if error:
        return render(
            request, "dashboard/settings/billing/_tariff_form.html",
            _tariff_form_ctx(
                club,
                tariff,
                error,
                component_rows=component_rows,
                use_components=use_components,
                personal_default_checked=is_personal_booking_default,
                personal_booking_trainer_id=personal_booking_trainer_id,
            ),
        )

    try:
        components_arg = component_payloads
        if tariff and use_components and component_payloads:
            price_changed = price != tariff.price
            if not price_changed and _component_payloads_equal(
                club=club,
                tariff=tariff,
                payloads=component_payloads,
            ):
                components_arg = None
        elif tariff and not use_components and _tariff_has_custom_components(club, tariff):
            components_arg = _default_component_payload(
                name=name,
                training_type_id=training_type_id,
                price=price,
                trainings_limit=trainings_limit,
                trainer_payout_policy=trainer_payout_policy,
                scope=scope,
                location_id=location_id,
            )

        if tariff:
            fields: dict = {
                "name": name,
                "description": description,
                "duration_days": duration_days,
            }
            if trainings_limit != tariff.trainings_limit:
                fields["trainings_limit"] = trainings_limit
            if price != tariff.price:
                fields["price"] = price
            if trainer_payout_policy != tariff.trainer_payout_policy:
                fields["trainer_payout_policy"] = trainer_payout_policy
            if is_personal_booking_default != tariff.is_personal_booking_default:
                fields["is_personal_booking_default"] = is_personal_booking_default
            if personal_booking_trainer_id != tariff.personal_booking_trainer_id:
                fields["personal_booking_trainer_id"] = personal_booking_trainer_id
            update_tariff(
                tariff_id=tariff.id,
                club_id=club.id,
                components=components_arg,
                **fields,
            )
        else:
            create_tariff(
                club_id=club.id,
                name=name,
                training_type_id=training_type_id,
                price=price,
                trainings_limit=trainings_limit,
                duration_days=duration_days,
                scope=scope,
                location_id=location_id,
                description=description,
                trainer_payout_policy=trainer_payout_policy,
                is_personal_booking_default=is_personal_booking_default,
                personal_booking_trainer_id=personal_booking_trainer_id,
                components=components_arg,
            )
    except BusinessLogicError as e:
        return render(
            request, "dashboard/settings/billing/_tariff_form.html",
            _tariff_form_ctx(
                club,
                tariff,
                str(e),
                component_rows=component_rows,
                use_components=use_components,
                personal_default_checked=is_personal_booking_default,
                personal_booking_trainer_id=personal_booking_trainer_id,
            ),
        )

    return HttpResponse(
        status=204, headers={"HX-Redirect": "/dashboard/settings/billing/"}
    )


@management_view_required
@require_POST
def tariff_toggle(request: HttpRequest, tariff_id: int) -> HttpResponse:
    """Toggle tariff is_active."""
    tariff = Tariff.objects.for_club(request.club).filter(id=tariff_id).first()
    if not tariff:
        raise Http404
    try:
        update_tariff(
            tariff_id=tariff_id,
            club_id=request.club.id,
            is_active=not tariff.is_active,
        )
    except BusinessLogicError as e:
        logger.warning(
            "tariff_toggle_failed",
            extra={"tariff_id": tariff_id, "error": str(e)},
        )
        return _render_billing_tab(request, error=str(e))
    return _render_billing_tab(request)


def _tariff_price_revision_form_context(
    *,
    tariff: Tariff,
    error: str | None = None,
    idempotency_key: str | None = None,
    new_name: str | None = None,
    new_price: str | None = None,
) -> dict:
    return {
        "tariff": tariff,
        "error": error,
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "new_name": tariff.name if new_name is None else new_name,
        "new_price": tariff.price if new_price is None else new_price,
    }


@management_view_required
def tariff_price_revision_form(request: HttpRequest, tariff_id: int) -> HttpResponse:
    """GET: render the price-change action; POST: create a new catalogue version."""
    tariff_queryset = (
        Tariff.objects.for_club(request.club)
        .select_related("training_type", "location", "personal_booking_trainer")
    )
    tariff = tariff_queryset.filter(
        id=tariff_id,
        **({"is_active": True} if request.method == "GET" else {}),
    ).first()
    if tariff is None:
        raise Http404
    if request.method == "GET":
        return render(
            request,
            "dashboard/settings/billing/_tariff_price_revision_form.html",
            _tariff_price_revision_form_context(tariff=tariff),
        )

    new_name = request.POST.get("new_name", "").strip()
    new_price_raw = request.POST.get("new_price", "").strip()
    idempotency_key = request.POST.get("idempotency_key", "").strip() or str(uuid.uuid4())
    try:
        new_price = Decimal(new_price_raw)
    except (InvalidOperation, ValueError):
        return render(
            request,
            "dashboard/settings/billing/_tariff_price_revision_form.html",
            _tariff_price_revision_form_context(
                tariff=tariff,
                error="Некорректная цена",
                idempotency_key=idempotency_key,
                new_name=new_name,
                new_price=new_price_raw,
            ),
        )
    try:
        revise_tariff_price(
            club_id=request.club.id,
            source_tariff_id=tariff.id,
            new_price=new_price,
            new_name=new_name,
            actor_user_id=request.user.id,
            idempotency_key=idempotency_key,
        )
    except BusinessLogicError as exc:
        return render(
            request,
            "dashboard/settings/billing/_tariff_price_revision_form.html",
            _tariff_price_revision_form_context(
                tariff=tariff,
                error=exc.message,
                idempotency_key=idempotency_key,
                new_name=new_name,
                new_price=new_price_raw,
            ),
        )
    return HttpResponse(status=204, headers={"HX-Redirect": "/dashboard/settings/billing/"})


@management_view_required
def tariff_archive_view(request: HttpRequest) -> HttpResponse:
    """Show inactive catalogue versions and manually archived tariffs."""
    ctx = _settings_context("billing", request)
    component_qs = (
        TariffComponent.objects.for_club(request.club)
        .filter(is_active=True)
        .select_related("training_type", "location")
        .order_by("sort_order", "id")
    )
    archived_tariffs = list(
        Tariff.objects.for_club(request.club)
        .filter(is_active=False)
        .select_related("training_type", "location", "personal_booking_trainer")
        .prefetch_related(Prefetch("components", queryset=component_qs, to_attr="active_components"))
        .order_by("-updated_at", "name")
    )
    for tariff in archived_tariffs:
        _annotate_tariff_for_admin_list(tariff)
    ctx["archived_tariffs"] = archived_tariffs
    template = "dashboard/settings/billing/tariff_archive.html"
    if request.htmx:
        return render(request, f"{template}#content", ctx)
    return render(request, template, ctx)


# ── Billing CRUD: Discounts ────────────────────────────────────────────


@management_view_required
def discount_form(
    request: HttpRequest, discount_id: int | None = None
) -> HttpResponse:
    """GET: show create/edit form in slide-over. POST: save discount."""
    club = request.club
    discount = None
    if discount_id:
        discount = Discount.objects.for_club(club).filter(id=discount_id).first()
        if not discount:
            raise Http404

    if request.method == "GET":
        ctx = {"discount": discount}
        return render(
            request, "dashboard/settings/billing/_discount_form.html", ctx
        )

    # POST
    name = request.POST.get("name", "").strip()
    discount_type = request.POST.get("discount_type", "").strip()
    value_str = request.POST.get("value", "").strip()

    error = None
    if not name:
        error = "Укажите название скидки"

    if not error and discount_type not in ("percent", "fixed"):
        error = "Выберите тип скидки"

    value = None
    if not error:
        try:
            value = Decimal(value_str)
            if value <= 0:
                error = "Значение должно быть больше 0"
            elif discount_type == "percent" and value > 100:
                error = "Процент не может быть больше 100"
        except (InvalidOperation, ValueError):
            error = "Некорректное значение"

    if error:
        ctx = {"discount": discount, "error": error}
        return render(
            request, "dashboard/settings/billing/_discount_form.html", ctx
        )

    try:
        if discount:
            update_discount(
                discount_id=discount.id,
                club_id=club.id,
                name=name,
                discount_type=discount_type,
                value=value,
            )
        else:
            create_discount(
                club_id=club.id,
                name=name,
                discount_type=discount_type,
                value=value,
            )
    except BusinessLogicError as e:
        ctx = {"discount": discount, "error": str(e)}
        return render(
            request, "dashboard/settings/billing/_discount_form.html", ctx
        )

    return HttpResponse(
        status=204, headers={"HX-Redirect": "/dashboard/settings/billing/"}
    )


@management_view_required
@require_POST
def discount_toggle(request: HttpRequest, discount_id: int) -> HttpResponse:
    """Toggle discount is_active."""
    discount = Discount.objects.for_club(request.club).filter(id=discount_id).first()
    if not discount:
        raise Http404
    try:
        update_discount(
            discount_id=discount_id,
            club_id=request.club.id,
            is_active=not discount.is_active,
        )
    except BusinessLogicError as e:
        logger.warning(
            "discount_toggle_failed",
            extra={"discount_id": discount_id, "error": str(e)},
        )
    return _render_billing_tab(request)
