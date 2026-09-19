from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest
from django.utils import timezone

from apps.attendance.models import Checkin, ScheduleEnrollment, StudentAttendanceCorrection
from apps.attendance.tests.factories import ScheduleFactory
from apps.billing.models import Payment, Subscription, SubscriptionCorrection, Tariff, TariffComponent, TrainingType
from apps.billing.service_modules.entitlements import refresh_subscription_counters
from apps.billing.service_modules.payment_review import verify_payment
from apps.billing.service_modules.tariff_revisions import revise_tariff_price
from apps.billing.tests.factories import (
    PaymentFactory,
    SubscriptionComponentFactory,
    SubscriptionFactory,
    TariffComponentFactory,
    TariffFactory,
    TrainingTypeFactory,
)
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerRateFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def card_case(club, owner_user, client, settings, monkeypatch):
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = True
    monkeypatch.setattr("apps.attendance.services.async_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("django_q.tasks.async_task", lambda *args, **kwargs: None)
    student = StudentFactory(club=club, status="active")
    subscription = SubscriptionFactory(
        club=club,
        student=student,
        tariff__club=club,
        tariff__training_type__club=club,
        trainings_left=7,
        trainings_used=1,
    )
    component = SubscriptionComponentFactory(
        club=club,
        subscription=subscription,
        credits_total=8,
        credits_used=1,
        credits_left=7,
        paid_amount_basis_snapshot=Decimal("8000"),
        unit_amount_basis_snapshot=Decimal("1000"),
        trainer_payout_policy_snapshot="on_checkin",
    )
    Subscription.objects.filter(id=subscription.id).update(activated_at=timezone.now() - timedelta(days=30))
    PaymentFactory(
        club=club,
        student=student,
        tariff=subscription.tariff,
        subscription=subscription,
        amount=Decimal("8000"),
        status="confirmed",
        verified_at=timezone.now() - timedelta(days=25),
    )
    day = timezone.now().date() - timedelta(days=2)
    schedule = ScheduleFactory(
        club=club, training_type=component.training_type, one_time_date=day, day_of_week=day.weekday()
    )
    TrainerRateFactory(
        club=club,
        trainer=schedule.trainer,
        location=schedule.location,
        training_type=schedule.training_type,
        percent=Decimal("50"),
    )
    ScheduleEnrollment.objects.create(
        club=club, student=student, schedule=schedule, starts_on=day - timedelta(days=30), status="active"
    )
    client.force_login(owner_user)
    return student, subscription, component, schedule


def correction_form(client, card_case):
    student, sub, component, _ = card_case
    url = f"/dashboard/students/{student.id}/subscriptions/{sub.id}/correct/"
    response = client.get(url, {"component_id": component.id})
    assert response.status_code == 200
    data = dict(
        component_id=component.id,
        command_key=response.context["command_key"],
        expected_fingerprint=response.context["fingerprint"],
        remaining=9,
        expires_on=response.context["expires_on"],
        initial_expires_on=response.context["initial_expires_on"],
        reason="Сверено с журналом",
    )
    return url, data


def test_card_exposes_context_actions_and_archive(client, card_case):
    student, sub, component, _ = card_case
    response = client.get(f"/dashboard/students/{student.id}/card/")
    assert response.status_code == 200
    text = response.content.decode()
    assert "Отметить посещение" in text and "Принять оплату" in text
    assert f"component_id={component.id}" in text and "Осталось 7 занятий" in text
    sub.status = "expired"
    sub.save()
    response = client.get(f"/dashboard/students/{student.id}/card/")
    assert "Архив абонементов (1)" in response.content.decode()


def test_card_correction_preserves_money_used_expiry_and_replays(client, card_case, settings):
    student, sub, component, _ = card_case
    expires = sub.expires_at
    url, data = correction_form(client, card_case)
    response = client.post(url, data)
    assert response.status_code == 200 and "Исправление сохранено" in response.content.decode()
    component.refresh_from_db()
    sub.refresh_from_db()
    assert (component.credits_left, component.credits_used) == (9, 1)
    assert component.unit_amount_basis_snapshot == Decimal("1000") and sub.expires_at == expires
    assert response.headers["HX-Trigger"] == "studentUpdated"
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = False
    response = client.post(url, data)
    assert "Исправление сохранено" in response.content.decode()
    assert SubscriptionCorrection.objects.filter(subscription=sub).count() == 1


def test_card_stale_correction_keeps_input_and_requires_resubmission(client, card_case):
    _, sub, component, _ = card_case
    url, data = correction_form(client, card_case)
    component.credits_left, component.credits_used = 6, 2
    component.save()
    refresh_subscription_counters(subscription=sub)
    response = client.post(url, data)
    assert response.status_code == 200
    assert "Ваш ввод сохранён" in response.content.decode()
    assert response.context["remaining"] == "9" and response.context["current_remaining"] == 6
    assert response.context["fingerprint"] != data["expected_fingerprint"]
    assert not SubscriptionCorrection.objects.filter(subscription=sub).exists()


def test_card_attendance_preview_apply_replay_and_cancel(client, card_case):
    student, sub, component, schedule = card_case
    url = f"/dashboard/students/{student.id}/attendance/record/"
    data = dict(
        date=str(schedule.one_time_date),
        schedule_id=schedule.id,
        entitlement=str(component.id),
        reason="Проверен журнал",
        command_key="card-visit",
        action="preview",
    )
    preview = client.post(url, data)
    assert preview.status_code == 200
    assert preview.context["can_apply"] and preview.context["salary_amount"] == Decimal("500")
    data.update(action="apply", expected_fingerprint=preview.context["preview"]["fingerprint"])
    result = client.post(url, data)
    assert "отмечено" in result.content.decode()
    repeat = client.post(url, data)
    assert "отмечено" in repeat.content.decode()
    checkin = Checkin.objects.get(student=student, deleted_at__isnull=True)
    assert StudentAttendanceCorrection.objects.filter(checkin=checkin, action="record").count() == 1
    cancel_url = f"/dashboard/students/{student.id}/attendance/{checkin.id}/cancel/"
    form = client.get(cancel_url)
    cancelled = client.post(cancel_url, {"reason": "Ошибка журнала", "command_key": form.context["command_key"]})
    assert "отменена" in cancelled.content.decode()
    component.refresh_from_db()
    assert (component.credits_left, component.credits_used) == (7, 1)


def test_card_new_actions_deny_other_tenant_and_trainer(client, card_case, other_club, trainer_user):
    student, sub, component, _ = card_case
    foreign = StudentFactory(club=other_club, status="active")
    response = client.get(f"/dashboard/students/{foreign.id}/subscriptions/{sub.id}/correct/")
    assert response.status_code == 404
    client.force_login(trainer_user)
    response = client.get(
        f"/dashboard/students/{student.id}/subscriptions/{sub.id}/correct/", {"component_id": component.id}
    )
    assert response.status_code == 403


def test_card_payment_entry_preserves_selected_student(client, card_case):
    student, _, _, _ = card_case
    response = client.get("/dashboard/billing/subscriptions/", {"student_id": student.id, "new": "1"})
    assert response.status_code == 200 and response.context["show_create_form"]
    assert f'<option value="{student.id}" selected>' in response.content.decode()


def test_card_history_renders_receipt_reason_and_actor(client, card_case):
    student, _, _, _ = card_case
    url, data = correction_form(client, card_case)
    client.post(url, data)
    result = client.get(f"/dashboard/students/{student.id}/history/?kind=corrections")
    assert result.status_code == 200 and "Сверено с журналом" in result.content.decode()


def test_card_exact_renewal_keeps_source_and_waits_for_verification(client, card_case):
    from apps.billing.models import Payment
    from apps.trainers.models import TrainerPackageAllocation

    student, sub, component, schedule = card_case
    TrainerPackageAllocation.objects.create(
        club=sub.club,
        subscription=sub,
        student=student,
        tariff=sub.tariff,
        training_type=component.training_type,
        owner_trainer=schedule.trainer,
        amount_snapshot=Decimal("8000"),
        sessions_total_snapshot=8,
        sessions_remaining_snapshot=7,
    )
    url = f"/dashboard/students/{student.id}/subscriptions/{sub.id}/renew/"
    form = client.get(url)
    command = {"command_key": form.context["command_key"], "payment_method": "cash"}
    result = client.post(url, command)
    assert result.status_code == 200 and "Продление №" in result.content.decode()
    payment = Payment.objects.get(subscription__renewed_from=sub)
    assert payment.status == "pending" and payment.subscription.status == "pending"
    assert payment.package_owner_trainer_id == schedule.trainer_id
    client.post(url, command)
    assert Payment.objects.filter(subscription__renewed_from=sub).count() == 1


def _revised_renewal_case(*, club, owner_user, student):
    training_type = TrainingTypeFactory(club=club, kind=TrainingType.Kind.GROUP)
    source_tariff = TariffFactory(
        club=club,
        training_type=training_type,
        name="HTMX source",
        price=Decimal("8000"),
        trainings_limit=8,
        duration_days=30,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    component = TariffComponentFactory(
        club=club,
        tariff=source_tariff,
        training_type=training_type,
        entitlement_kind=TariffComponent.EntitlementKind.FINITE_CREDITS,
        credits_total=8,
        paid_amount_basis=source_tariff.price,
        trainer_payout_policy=Tariff.PayoutPolicy.ON_PAYMENT,
    )
    source = SubscriptionFactory(
        club=club,
        student=student,
        tariff=source_tariff,
        status=Subscription.Status.ACTIVE,
        trainings_left=7,
        trainings_used=1,
        expires_at=timezone.now() + timedelta(days=7),
    )
    SubscriptionComponentFactory(
        club=club,
        subscription=source,
        tariff_component=component,
        credits_left=7,
    )
    revision = revise_tariff_price(
        club_id=club.id,
        source_tariff_id=source_tariff.id,
        new_price=Decimal("8500"),
        new_name="HTMX target",
        actor_user_id=owner_user.id,
        idempotency_key=f"htmx-renewal-a-b-{student.id}",
    )
    return source, revision


@pytest.mark.django_db(transaction=True)
def test_card_stale_offer_error_rerenders_without_select_for_update_outside_atomic(
    client,
    club,
    owner_user,
    settings,
):
    settings.STUDENT_ADMIN_CORRECTIONS_ENABLED = True
    student = StudentFactory(club=club, status="active")
    source, revision = _revised_renewal_case(club=club, owner_user=owner_user, student=student)
    url = f"/dashboard/students/{student.id}/subscriptions/{source.id}/renew/"
    client.force_login(owner_user)
    form = client.get(url)
    assert form.status_code == 200
    assert 'name="expected_target_price" value="8500.00"' in form.content.decode()
    Tariff.objects.filter(id=revision.target_tariff_id).update(is_active=False)

    response = client.post(
        url,
        {
            "command_key": "htmx-stale-offer",
            "payment_method": Payment.Method.CASH,
            "expected_target_tariff_id": revision.target_tariff_id,
            "expected_target_price": "8500.00",
        },
    )

    assert response.status_code == 200
    assert "Тариф продления больше недоступен" in response.content.decode()
    assert Payment.objects.filter(subscription__renewed_from=source).count() == 0


@pytest.mark.django_db(transaction=True)
def test_card_accepted_revision_replays_after_target_leaf_archived(
    client,
    club,
    owner_user,
):
    student = StudentFactory(club=club, status="active")
    source, revision = _revised_renewal_case(club=club, owner_user=owner_user, student=student)
    url = f"/dashboard/students/{student.id}/subscriptions/{source.id}/renew/"
    client.force_login(owner_user)
    payload = {
        "command_key": "htmx-accepted-archived-leaf",
        "payment_method": Payment.Method.CASH,
        "expected_target_tariff_id": revision.target_tariff_id,
        "expected_target_price": "8500.00",
    }
    with patch("django_q.tasks.async_task"):
        created = client.post(url, payload)
        assert created.status_code == 200, created.content.decode()
        payment = Payment.objects.get(subscription__renewed_from=source)
        verify_payment(
            payment_id=payment.id,
            club_id=club.id,
            verified_by_id=owner_user.id,
            action="confirm",
        )
    Tariff.objects.filter(id=revision.target_tariff_id).update(is_active=False)

    replay = client.post(url, payload)

    assert replay.status_code == 200
    assert "Продление №" in replay.content.decode()
    assert Payment.objects.filter(subscription__renewed_from=source).count() == 1


def test_card_expiry_form_replay_and_frozen_block(client, card_case):
    _, sub, _, _ = card_case
    url, data = correction_form(client, card_case)
    data["expires_on"] = (timezone.now().date() + timedelta(days=60)).isoformat()
    assert "Исправление сохранено" in client.post(url, data).content.decode()
    assert "Исправление сохранено" in client.post(url, data).content.decode()
    sub.refresh_from_db()
    sub.status = "frozen"
    sub.save()
    result = client.get(url)
    assert not result.context["can_save"]
    assert result.context["error"]


def test_card_attendance_without_rate_requires_review(client, card_case):
    from apps.trainers.models import TrainerRate

    student, _, component, schedule = card_case
    TrainerRate.objects.for_club(student.club).all().delete()
    result = client.post(
        f"/dashboard/students/{student.id}/attendance/record/",
        {
            "date": str(schedule.one_time_date),
            "schedule_id": schedule.id,
            "entitlement": str(component.id),
            "action": "preview",
            "reason": "По журналу",
        },
    )
    assert result.status_code == 200 and not result.context["can_apply"]
    assert "не задана ставка" in result.context["error"]
    assert not Checkin.objects.filter(student=student).exists()


def test_disposable_student_operations_fixture_is_complete():
    from apps.common.management.commands.prepare_student_operations_e2e import Command
    from apps.trainers.models import TrainerPackageAllocation

    fixture = Command().create_fixture()
    subscription = Subscription.objects.get(id=fixture["subscription_id"])
    assert (subscription.trainings_left, subscription.trainings_used) == (7, 1)
    assert subscription.payment.original_amount == Decimal("8000")
    assert TrainerPackageAllocation.objects.filter(subscription=subscription).count() == 1
    assert ScheduleEnrollment.objects.filter(student=subscription.student).count() == 2


@pytest.mark.parametrize("kind", ["unlimited", "weekly_limit"])
def test_card_current_nonfinite_attendance_and_weekly_capacity(client, card_case, kind, monkeypatch):
    from apps.clubs.timezones import club_localdate, club_zoneinfo

    student, sub, component, schedule = card_case
    day = club_localdate(student.club)
    # Current attendance is intentionally tested inside the actual slot window.
    observed_at = timezone.make_aware(datetime.combine(day, schedule.start_time), club_zoneinfo(student.club))
    monkeypatch.setattr(timezone, "now", lambda: observed_at)
    schedule.one_time_date, schedule.day_of_week = day, day.weekday()
    schedule.save()
    component.entitlement_kind = kind
    component.credits_total = component.credits_left = None
    component.weekly_limit = 1 if kind == "weekly_limit" else None
    component.trainer_payout_policy_snapshot = "on_payment"
    component.save()
    refresh_subscription_counters(subscription=sub)
    url = f"/dashboard/students/{student.id}/attendance/record/"
    data = dict(
        date=str(day),
        schedule_id=schedule.id,
        entitlement=str(component.id),
        reason="По журналу",
        command_key=f"card-{kind}",
        action="preview",
    )
    preview = client.post(url, data)
    assert preview.context["can_apply"], preview.context["error"]
    data.update(action="apply", expected_fingerprint=preview.context["preview"]["fingerprint"])
    assert "отмечено" in client.post(url, data).content.decode()
    component.refresh_from_db()
    assert component.credits_left is None
    assert Checkin.objects.filter(student=student, subscription_component=component).count() == 1
    if kind == "weekly_limit":
        second = ScheduleFactory(
            club=student.club,
            training_type=component.training_type,
            one_time_date=day,
            day_of_week=day.weekday(),
            start_time=schedule.start_time,
            end_time=schedule.end_time,
        )
        ScheduleEnrollment.objects.create(
            club=student.club, student=student, schedule=second, starts_on=day, status="active"
        )
        data.update(action="preview", schedule_id=second.id, command_key="weekly-second")
        blocked = client.post(url, data)
        assert not blocked.context["can_apply"]
        assert "Лимит посещений" in blocked.context["error"]


def test_card_date_refresh_uses_post_and_preserves_private_form_input(client, card_case):
    student, _, _, schedule = card_case
    url = f"/dashboard/students/{student.id}/attendance/record/"
    response = client.post(
        url,
        {
            "action": "options",
            "date": str(schedule.one_time_date),
            "reason": "Сверка журнала",
            "command_key": "keep-key",
        },
    )
    assert response.status_code == 200 and not response.context.get("error")
    assert response.context["reason"] == "Сверка журнала"
    assert response.context["command_key"] == "keep-key"
    assert [o.schedule_id for o in response.context["occurrences"]] == [schedule.id]
    assert 'hx-vals=\'{"action":"options"}\'' in response.content.decode()
    assert not Checkin.objects.filter(student=student).exists()


def test_student_operations_fixture_refuses_non_disposable_database(monkeypatch):
    from django.core.management.base import CommandError

    from apps.common.management.commands import prepare_student_operations_e2e as fixture_module

    monkeypatch.setitem(fixture_module.connection.settings_dict, "NAME", "club_live")
    with pytest.raises(CommandError, match="disposable"):
        fixture_module.assert_disposable_database()


@pytest.mark.parametrize("foreign", [False, True])
def test_attendance_missing_schedule_keeps_input(client, card_case, other_club, foreign):
    student, _, component, schedule = card_case
    schedule_id = ScheduleFactory(club=other_club).id if foreign else 2147483647
    result = client.post(f"/dashboard/students/{student.id}/attendance/record/", {
        "date": str(schedule.one_time_date), "schedule_id": schedule_id,
        "entitlement": str(component.id), "action": "preview", "reason": "Сохранить ввод",
    })
    assert result.status_code == 200
    assert "Занятие не найдено" in result.context["error"]
    assert result.context["reason"] == "Сохранить ввод"
    assert not Checkin.objects.filter(student=student).exists()


@pytest.mark.parametrize("remaining", [7, 9])
def test_history_shows_balance_expiry_component_and_channel(client, card_case, remaining):
    student, _, component, _ = card_case
    url, data = correction_form(client, card_case)
    data["expires_on"] = (timezone.now().date() + timedelta(days=60)).isoformat()
    data["remaining"] = remaining
    client.post(url, data)
    result = client.get(f"/dashboard/students/{student.id}/history/?kind=corrections")
    text = result.content.decode()
    assert "Срок:" in text and "изменение остатка +0" not in text
    if remaining == 9:
        assert "Остаток: 7 → 9" in text
    assert f"компонент №{component.id}" in text and "канал admin" in text
    assert SubscriptionCorrection.objects.filter(subscription__student=student).count() == 1


def test_student_freeze_preserves_input_and_returns_card(client, card_case, monkeypatch):
    from apps.common.exceptions import BusinessLogicError

    student, sub, _, _ = card_case
    url = f"/dashboard/billing/subscriptions/{sub.id}/freeze/?student_context=1"
    def fail(**kwargs):
        raise BusinessLogicError("Заморозка недоступна")
    monkeypatch.setattr("apps.htmx_admin.views.billing.billing_freeze_subscription", fail)
    result = client.post(url, {"days": "12", "reason": "injury"})
    assert result.status_code == 200
    assert result.context["days"] == "12" and result.context["reason"] == "injury"
    assert 'value="12"' in result.content.decode()
    assert 'value="injury" selected' in result.content.decode()
    assert f'/dashboard/students/{student.id}/card/' in result.content.decode()
    monkeypatch.setattr("apps.htmx_admin.views.billing.billing_freeze_subscription", lambda **kwargs: None)
    result = client.post(url, {"days": "12", "reason": "injury"})
    assert result.headers["HX-Trigger"] == "studentUpdated"
    assert "Абонемент заморожен" in result.content.decode()


def test_frozen_student_panel_offers_thaw(client, card_case, owner_user):
    from apps.billing.models import SubscriptionFreeze
    student, sub, _, _ = card_case
    sub.status = "frozen"
    sub.save()
    result = client.get(f"/dashboard/billing/subscriptions/{sub.id}/freeze/?student_context=1")
    assert 'value="thaw"' in result.content.decode()
    assert 'name="days"' not in result.content.decode()
    card = client.get(f"/dashboard/students/{student.id}/card/")
    assert "Разморозить" in card.content.decode()
    freeze = SubscriptionFreeze.objects.create(
        club=student.club, subscription=sub, days=12, reason="injury",
        frozen_by=owner_user, starts_at=timezone.now() - timedelta(days=2),
    )
    result = client.post(f"/dashboard/billing/subscriptions/{sub.id}/freeze/", {
        "student_context": "1", "action": "thaw",
    })
    assert result.status_code == 200 and result.headers["HX-Trigger"] == "studentUpdated"
    assert "Абонемент разморожен" in result.content.decode()
    freeze.refresh_from_db()
    assert freeze.ends_at is not None



def test_student_list_has_keyboard_openers_and_attendance_disables_all_submits(client, card_case):
    student, _, _, _ = card_case
    listing = client.get("/dashboard/students/", {"q": student.last_name}).content.decode()
    assert 'role="button" tabindex="0" data-student-opener' in listing
    assert '<button type="button" data-student-opener' in listing
    form = client.get(f"/dashboard/students/{student.id}/attendance/record/").content.decode()
    assert 'hx-disabled-elt="#slide-over form button[type=\'submit\']"' in form
