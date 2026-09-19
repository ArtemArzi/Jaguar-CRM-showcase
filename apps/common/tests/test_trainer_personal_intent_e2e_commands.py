import io
import json
from datetime import date, datetime, time
from unittest.mock import patch

import pytest
from django.core.management import call_command

from apps.attendance.models import Checkin, PersonalAvailabilitySlot, PersonalDropInBooking
from apps.attendance.personal_offers import personal_offer_payload
from apps.attendance.services.checkin import create_checkin
from apps.attendance.services.drop_in import (
    create_personal_drop_in_bank_payment_order,
    create_personal_drop_in_payment,
)
from apps.attendance.services.personal_payment_corrections import replace_personal_payment_method
from apps.attendance.services.personal_reschedule import reschedule_personal_exact_booking
from apps.attendance.services.staff_intents import (
    get_staff_direct_personal_offer,
    submit_staff_direct_personal_intent,
    submit_staff_personal_intent,
)
from apps.billing.models import Tariff, TariffComponent
from apps.billing.service_modules.personal_offers import resolve_personal_booking_offer
from apps.billing.services import cancel_bank_payment_order
from apps.clubs.models import ClubSettings


def _fixture(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _offer_digest(*, fixture, slot_key, discount_id=None):
    slot = PersonalAvailabilitySlot.objects.get(id=fixture[slot_key]["id"])
    offer = resolve_personal_booking_offer(
        club_id=fixture["club_id"],
        training_type_id=fixture["personal_training_type"]["id"],
        location_id=fixture["location"]["id"],
        trainer_id=fixture["trainer"]["trainer_id"],
        discount_id=discount_id,
    )
    return personal_offer_payload(slot=slot, offer=offer)["offer_digest"]


@pytest.mark.django_db
def test_prepare_trainer_personal_intent_e2e_creates_complete_flag_on_fixture(tmp_path):
    output = tmp_path / "fixture.json"
    stdout = io.StringIO()

    call_command("prepare_trainer_personal_intent_e2e", output=str(output), stdout=stdout)

    fixture = _fixture(output)
    stdout_text = stdout.getvalue()
    tariff = Tariff.objects.get(id=fixture["personal_tariff"]["id"])
    component = TariffComponent.objects.get(tariff=tariff)
    entitlement_id = fixture["entitlement_student"]["subscription_id"]

    assert ClubSettings.objects.get(club_id=fixture["club_id"]).unified_client_journey_enabled is True
    assert fixture["club_timezone"] == "Asia/Yekaterinburg"
    assert tariff.is_personal_booking_default is True
    assert tariff.price == component.paid_amount_basis
    assert component.credits_total == 1
    assert component.is_active is True
    assert PersonalAvailabilitySlot.objects.filter(
        id__in=[
            fixture["cash_slot"]["id"],
            fixture["sbp_slot"]["id"],
            fixture["entitlement_slot"]["id"],
            fixture["pay_at_visit_slot"]["id"],
            fixture["pay_at_visit_sbp_slot"]["id"],
            fixture["terminal_sbp_slot"]["id"],
            fixture["correction_slot"]["id"],
            fixture["correction_destination_slot"]["id"],
        ],
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    ).count() == 8
    assert entitlement_id
    assert fixture["pay_at_visit_student"]["student_id"]
    assert fixture["pay_at_visit_sbp_student"]["student_id"]
    assert fixture["terminal_sbp_student"]["student_id"]
    assert fixture["direct_booking"]["date"]
    assert fixture["direct_terminal_booking"]["starts_at_utc"].endswith("+00:00")
    assert fixture["direct_terminal_sbp_student"]["student_id"]
    assert fixture["correction_student"]["student_id"]
    assert fixture["direct_correction_student"]["student_id"]
    assert fixture["direct_correction_booking"]["date"]
    assert fixture["personal_discount"]["value"] == "500.00"
    assert fixture["expected"]["discounted_amount"] == "2200.00"
    assert fixture["kiosk"]["activation_pin"].isdigit()
    assert fixture["expected"]["entitlement_status"] == "Записан"
    assert fixture["expected"]["pay_at_visit_status"] == "К оплате при посещении"
    assert fixture["expected"]["debt_open_status"] == "Долг ожидает оплаты"
    assert fixture["trainer"]["password"] not in stdout_text
    assert "password" not in stdout_text.lower()


@pytest.mark.django_db
def test_assert_trainer_personal_intent_e2e_checks_full_staff_intent_fixture(tmp_path, settings):
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.DEBUG = True
    settings.PAYMENT_PROVIDER = "mock"
    settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
    settings.MOCK_PAYMENT_WEBHOOKS_ENABLED = True
    settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
    settings.JAGUAR_PAYMENT_RETURN_ORIGIN = "http://127.0.0.1:4174"
    output = tmp_path / "fixture.json"
    call_command("prepare_trainer_personal_intent_e2e", output=str(output), stdout=io.StringIO())
    fixture = _fixture(output)

    for payment_method, student_key, slot_key in (
        ("cash", "cash_lead", "cash_slot"),
        ("sbp", "sbp_student", "sbp_slot"),
        ("entitlement", "entitlement_student", "entitlement_slot"),
        ("pay_at_visit", "pay_at_visit_student", "pay_at_visit_slot"),
    ):
        submit_staff_personal_intent(
            club_id=fixture["club_id"],
            slot_id=fixture[slot_key]["id"],
            student_id=fixture[student_key]["student_id"],
            payment_method=payment_method,
            offer_digest=(
                None
                if payment_method == "entitlement"
                else _offer_digest(
                    fixture=fixture,
                    slot_key=slot_key,
                    discount_id=(
                        fixture["personal_discount"]["id"]
                        if payment_method == "pay_at_visit"
                        else None
                    ),
                )
            ),
            subscription_id=(
                fixture["entitlement_student"]["subscription_id"]
                if payment_method == "entitlement"
                else None
            ),
            idempotency_key=f"command-test-{payment_method}-{fixture['fixture_id']}",
            actor_user_id=fixture["trainer"]["user_id"],
            bank_source="trainer",
            discount_id=(
                fixture["personal_discount"]["id"]
                if payment_method == "pay_at_visit"
                else None
            ),
        )

    pay_at_visit_booking = PersonalDropInBooking.objects.get(
        enrollment__student_id=fixture["pay_at_visit_student"]["student_id"],
    )
    with patch("apps.attendance.services.async_task"):
        create_checkin(
            club_id=fixture["club_id"],
            student_id=fixture["pay_at_visit_student"]["student_id"],
            schedule_id=pay_at_visit_booking.enrollment.schedule_id,
            training_type_id=fixture["personal_training_type"]["id"],
            source=Checkin.Source.KIOSK,
            checkin_date=pay_at_visit_booking.enrollment.starts_on,
        )
    pay_at_visit_booking.refresh_from_db()
    assert pay_at_visit_booking.debt_id is not None
    create_personal_drop_in_payment(
        club_id=fixture["club_id"],
        booking_id=pay_at_visit_booking.id,
        payment_method="cash",
        created_by_id=fixture["trainer"]["user_id"],
        discount_ids=[],
        idempotency_key=f"command-test-pay-at-visit-settlement-{fixture['fixture_id']}",
        debt_id=pay_at_visit_booking.debt_id,
    )

    submit_staff_personal_intent(
        club_id=fixture["club_id"],
        slot_id=fixture["pay_at_visit_sbp_slot"]["id"],
        student_id=fixture["pay_at_visit_sbp_student"]["student_id"],
        payment_method="pay_at_visit",
        subscription_id=None,
        offer_digest=_offer_digest(fixture=fixture, slot_key="pay_at_visit_sbp_slot"),
        idempotency_key=f"command-test-pay-at-visit-sbp-{fixture['fixture_id']}",
        actor_user_id=fixture["trainer"]["user_id"],
        bank_source="trainer",
    )
    pay_at_visit_sbp_booking = PersonalDropInBooking.objects.get(
        enrollment__student_id=fixture["pay_at_visit_sbp_student"]["student_id"],
    )
    with patch("apps.attendance.services.async_task"):
        create_checkin(
            club_id=fixture["club_id"],
            student_id=fixture["pay_at_visit_sbp_student"]["student_id"],
            schedule_id=pay_at_visit_sbp_booking.enrollment.schedule_id,
            training_type_id=fixture["personal_training_type"]["id"],
            source=Checkin.Source.KIOSK,
            checkin_date=pay_at_visit_sbp_booking.enrollment.starts_on,
        )
    pay_at_visit_sbp_booking.refresh_from_db()
    assert pay_at_visit_sbp_booking.debt_id is not None
    create_personal_drop_in_bank_payment_order(
        club_id=fixture["club_id"],
        booking_id=pay_at_visit_sbp_booking.id,
        source="trainer",
        created_by_id=fixture["trainer"]["user_id"],
        idempotency_key=f"command-test-pay-at-visit-sbp-settlement-{fixture['fixture_id']}",
        debt_id=pay_at_visit_sbp_booking.debt_id,
    )

    terminal_result = submit_staff_personal_intent(
        club_id=fixture["club_id"],
        slot_id=fixture["terminal_sbp_slot"]["id"],
        student_id=fixture["terminal_sbp_student"]["student_id"],
        payment_method="sbp",
        subscription_id=None,
        offer_digest=_offer_digest(fixture=fixture, slot_key="terminal_sbp_slot"),
        idempotency_key=f"command-test-terminal-sbp-{fixture['fixture_id']}",
        actor_user_id=fixture["trainer"]["user_id"],
        bank_source="trainer",
    )
    cancel_bank_payment_order(
        club_id=fixture["club_id"],
        order_id=terminal_result.receipt["bank_payment_order_id"],
        actor_user_id=fixture["trainer"]["user_id"],
        allowed_sources={"trainer"},
    )
    submit_staff_personal_intent(
        club_id=fixture["club_id"],
        slot_id=fixture["terminal_sbp_slot"]["id"],
        student_id=fixture["terminal_sbp_student"]["student_id"],
        payment_method="sbp",
        subscription_id=None,
        offer_digest=_offer_digest(fixture=fixture, slot_key="terminal_sbp_slot"),
        idempotency_key=f"command-test-terminal-sbp-retry-{fixture['fixture_id']}",
        actor_user_id=fixture["trainer"]["user_id"],
        bank_source="trainer",
    )

    correction_result = submit_staff_personal_intent(
        club_id=fixture["club_id"],
        slot_id=fixture["correction_slot"]["id"],
        student_id=fixture["correction_student"]["student_id"],
        payment_method="sbp",
        subscription_id=None,
        offer_digest=_offer_digest(
            fixture=fixture,
            slot_key="correction_slot",
            discount_id=fixture["personal_discount"]["id"],
        ),
        idempotency_key=f"command-test-correction-sbp-{fixture['fixture_id']}",
        actor_user_id=fixture["trainer"]["user_id"],
        bank_source="trainer",
        discount_id=fixture["personal_discount"]["id"],
    )
    correction_replacement = replace_personal_payment_method(
        club_id=fixture["club_id"],
        student_id=fixture["correction_student"]["student_id"],
        actor_user_id=fixture["trainer"]["user_id"],
        actor_role="trainer",
        reservation_id=correction_result.receipt["reservation_id"],
        replacement_payment_method="pay_at_visit",
        reason="command fixture safe correction",
        idempotency_key=f"command-test-correction-replacement-{fixture['fixture_id']}",
    )
    correction_booking = PersonalDropInBooking.objects.get(id=correction_replacement["booking_id"])
    reschedule_personal_exact_booking(
        club_id=fixture["club_id"],
        enrollment_id=correction_booking.enrollment_id,
        destination_slot_id=fixture["correction_destination_slot"]["id"],
        actor_user_id=fixture["trainer"]["user_id"],
        reason="command fixture reschedule",
        idempotency_key=f"command-test-correction-reschedule-{fixture['fixture_id']}",
    )

    direct_date = date.fromisoformat(fixture["direct_booking"]["date"])
    direct_start = datetime.combine(direct_date, time.fromisoformat(fixture["direct_booking"]["start_time"]))
    direct_end = datetime.combine(direct_date, time.fromisoformat(fixture["direct_booking"]["end_time"]))
    direct_offer = get_staff_direct_personal_offer(
        club_id=fixture["club_id"],
        trainer_id=fixture["trainer"]["trainer_id"],
        starts_at=direct_start,
        ends_at=direct_end,
        location_id=fixture["location"]["id"],
        training_type_id=fixture["personal_training_type"]["id"],
    )
    submit_staff_direct_personal_intent(
        club_id=fixture["club_id"],
        student_id=fixture["direct_student"]["student_id"],
        trainer_id=fixture["trainer"]["trainer_id"],
        starts_at=direct_start,
        ends_at=direct_end,
        location_id=fixture["location"]["id"],
        training_type_id=fixture["personal_training_type"]["id"],
        payment_method="cash",
        subscription_id=None,
        offer_digest=direct_offer["offer_digest"],
        idempotency_key=f"command-test-direct-{fixture['fixture_id']}",
        actor_user_id=fixture["trainer"]["user_id"],
        bank_source="trainer",
    )

    direct_correction_date = date.fromisoformat(fixture["direct_correction_booking"]["date"])
    direct_correction_start = datetime.combine(
        direct_correction_date,
        time.fromisoformat(fixture["direct_correction_booking"]["start_time"]),
    )
    direct_correction_end = datetime.combine(
        direct_correction_date,
        time.fromisoformat(fixture["direct_correction_booking"]["end_time"]),
    )
    direct_correction_offer = get_staff_direct_personal_offer(
        club_id=fixture["club_id"],
        trainer_id=fixture["trainer"]["trainer_id"],
        starts_at=direct_correction_start,
        ends_at=direct_correction_end,
        location_id=fixture["location"]["id"],
        training_type_id=fixture["personal_training_type"]["id"],
        discount_id=fixture["personal_discount"]["id"],
    )
    direct_correction_result = submit_staff_direct_personal_intent(
        club_id=fixture["club_id"],
        student_id=fixture["direct_correction_student"]["student_id"],
        trainer_id=fixture["trainer"]["trainer_id"],
        starts_at=direct_correction_start,
        ends_at=direct_correction_end,
        location_id=fixture["location"]["id"],
        training_type_id=fixture["personal_training_type"]["id"],
        payment_method="sbp",
        subscription_id=None,
        offer_digest=direct_correction_offer["offer_digest"],
        discount_id=fixture["personal_discount"]["id"],
        idempotency_key=f"command-test-direct-correction-sbp-{fixture['fixture_id']}",
        actor_user_id=fixture["trainer"]["user_id"],
        bank_source="trainer",
    )
    replace_personal_payment_method(
        club_id=fixture["club_id"],
        student_id=fixture["direct_correction_student"]["student_id"],
        actor_user_id=fixture["trainer"]["user_id"],
        actor_role="trainer",
        reservation_id=direct_correction_result.receipt["reservation_id"],
        replacement_payment_method="cash",
        reason="command fixture direct correction",
        idempotency_key=f"command-test-direct-correction-replacement-{fixture['fixture_id']}",
    )

    direct_terminal_date = date.fromisoformat(fixture["direct_terminal_booking"]["date"])
    direct_terminal_start = datetime.combine(
        direct_terminal_date,
        time.fromisoformat(fixture["direct_terminal_booking"]["start_time"]),
    )
    direct_terminal_end = datetime.combine(
        direct_terminal_date,
        time.fromisoformat(fixture["direct_terminal_booking"]["end_time"]),
    )
    direct_terminal_offer = get_staff_direct_personal_offer(
        club_id=fixture["club_id"],
        trainer_id=fixture["trainer"]["trainer_id"],
        starts_at=direct_terminal_start,
        ends_at=direct_terminal_end,
        location_id=fixture["location"]["id"],
        training_type_id=fixture["personal_training_type"]["id"],
    )
    direct_terminal_result = submit_staff_direct_personal_intent(
        club_id=fixture["club_id"],
        student_id=fixture["direct_terminal_sbp_student"]["student_id"],
        trainer_id=fixture["trainer"]["trainer_id"],
        starts_at=direct_terminal_start,
        ends_at=direct_terminal_end,
        location_id=fixture["location"]["id"],
        training_type_id=fixture["personal_training_type"]["id"],
        payment_method="sbp",
        subscription_id=None,
        offer_digest=direct_terminal_offer["offer_digest"],
        idempotency_key=f"command-test-direct-terminal-sbp-{fixture['fixture_id']}",
        actor_user_id=fixture["trainer"]["user_id"],
        bank_source="trainer",
    )
    cancel_bank_payment_order(
        club_id=fixture["club_id"],
        order_id=direct_terminal_result.receipt["bank_payment_order_id"],
        actor_user_id=fixture["trainer"]["user_id"],
        allowed_sources={"trainer"},
    )
    submit_staff_direct_personal_intent(
        club_id=fixture["club_id"],
        student_id=fixture["direct_terminal_sbp_student"]["student_id"],
        trainer_id=fixture["trainer"]["trainer_id"],
        starts_at=direct_terminal_start,
        ends_at=direct_terminal_end,
        location_id=fixture["location"]["id"],
        training_type_id=fixture["personal_training_type"]["id"],
        payment_method="sbp",
        subscription_id=None,
        offer_digest=direct_terminal_offer["offer_digest"],
        idempotency_key=f"command-test-direct-terminal-sbp-retry-{fixture['fixture_id']}",
        actor_user_id=fixture["trainer"]["user_id"],
        bank_source="trainer",
    )

    stdout = io.StringIO()
    call_command(
        "assert_trainer_personal_intent_e2e",
        fixture=str(output),
        timeout_seconds=0,
        stdout=stdout,
    )
    evidence = json.loads(stdout.getvalue())

    assert evidence["ok"] is True
    assert evidence["cash"]["status"] == "pending"
    assert evidence["sbp"]["status"] in {"created", "pending"}
    assert evidence["entitlement"]["subscription_id"] == fixture["entitlement_student"]["subscription_id"]
    assert evidence["pay_at_visit"]["debt_id"]
    assert evidence["pay_at_visit_sbp"]["bank_payment_order_id"]
    assert evidence["terminal_sbp"]["live_bank_payment_order_id"]
    assert evidence["terminal_sbp"]["terminal_reservation_ids"]
    assert evidence["payment_correction"]["correction_id"]
    assert evidence["payment_correction"]["replacement_booking_id"]
    assert evidence["direct_payment_correction"]["replacement_booking_id"]
    assert evidence["direct"]["payment_id"]
    assert evidence["direct_terminal_sbp"]["live_bank_payment_order_id"]
