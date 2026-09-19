import json
from datetime import timedelta
from unittest.mock import patch

import pytest
from django.db import connection
from django.utils import timezone
from ninja.testing import TestClient

from apps.attendance.models import ScheduleEnrollment, TrainingGroup, TrainingGroupRolloutState
from apps.attendance.tests.factories import CheckinFactory, ScheduleFactory
from apps.attendance.tests.rollout import update_training_group_rollout_state_for_test
from apps.billing.models import BankPaymentOrder, Payment
from apps.billing.payment_providers.mock import MockPaymentProvider
from apps.billing.selectors import get_group_enrollment_options
from apps.billing.services import process_bank_payment_webhook
from apps.billing.tests.factories import TariffFactory, TrainingTypeFactory
from apps.clubs.tests.factories import ClubSettingsFactory, LocationFactory
from apps.common.tests.helpers import make_auth_params
from apps.students.models import Student
from apps.students.tests.factories import StudentFactory
from apps.trainers.tests.factories import TrainerFactory
from config.api import api

client = TestClient(api)


@pytest.fixture(autouse=True)
def _bypass_jwt_auth(bypass_jwt_auth):
    pass


def _enable_v2_group_sales(*, club, settings) -> None:
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = True
    settings.MANUAL_OPERATIONAL_ADMISSION_ENABLED = True
    settings.TRAINING_GROUP_NEW_WRITES_ENABLED = True
    ClubSettingsFactory(
        club=club,
        unified_client_journey_enabled=True,
        commercial_journey_protocol_version="v2",
    )
    rollout, _created = TrainingGroupRolloutState.objects.for_club(club).get_or_create(
        defaults={"mode": TrainingGroupRolloutState.Mode.OFF},
        club=club,
    )
    update_training_group_rollout_state_for_test(
        TrainingGroupRolloutState.objects.for_club(club).filter(id=rollout.id),
        mode=TrainingGroupRolloutState.Mode.ACTIVE,
    )


def _group_sale_context(*, club, student_status=Student.Status.LEAD):
    start_date = timezone.localdate() + timedelta(days=7)
    training_type = TrainingTypeFactory(club=club, kind="group")
    location = LocationFactory(club=club)
    responsible = TrainerFactory(club=club)
    group = TrainingGroup.objects.create(
        club=club,
        name="V2 group",
        training_type=training_type,
        location=location,
        responsible_trainer=responsible,
        status=TrainingGroup.Status.ACTIVE,
    )
    schedule = ScheduleFactory(
        club=club,
        training_group=group,
        trainer=responsible,
        location=location,
        training_type=training_type,
        day_of_week=start_date.weekday(),
    )
    return {
        "student": StudentFactory(
            club=club,
            status=student_status,
            lead_status=Student.LeadStatus.NEW,
            assigned_trainer=responsible,
        ),
        "tariff": TariffFactory(
            club=club,
            training_type=training_type,
            price=5500,
            trainings_limit=8,
            duration_days=30,
        ),
        "group": group,
        "schedule": schedule,
        "start_date": start_date,
    }


def _preview(*, club, owner_user, context):
    return client.get(
        (
            "/billing/group-sale-offers/preview/"
            f"?student_id={context['student'].id}"
            f"&tariff_id={context['tariff'].id}"
            f"&target_training_group_id={context['group'].id}"
            f"&target_schedule_id={context['schedule'].id}"
            f"&target_start_date={context['start_date'].isoformat()}"
        ),
        **make_auth_params(owner_user, club),
    )


def _manual_payload(*, context, offer_digest, idempotency_key):
    return {
        "protocol_version": "v2",
        "student_id": context["student"].id,
        "tariff_id": context["tariff"].id,
        "payment_method": "transfer",
        "target_training_group_id": context["group"].id,
        "target_schedule_id": context["schedule"].id,
        "target_start_date": context["start_date"].isoformat(),
        "expected_offer_digest": offer_digest,
        "idempotency_key": idempotency_key,
    }


@pytest.mark.django_db
def test_v2_group_offer_preview_is_complete_and_manual_submit_admits_the_lead(
    club,
    owner_user,
    settings,
):
    _enable_v2_group_sales(club=club, settings=settings)
    context = _group_sale_context(club=club)

    preview = _preview(club=club, owner_user=owner_user, context=context)

    assert preview.status_code == 200, preview.json()
    offer = preview.json()
    assert offer["offer_digest"]
    assert offer["tariff"]["price"] == "5500.00"
    assert offer["group"]["id"] == context["group"].id
    assert offer["selected_occurrence"]["schedule_id"] == context["schedule"].id
    assert offer["expected_action"] == "new_admission"

    with patch("django_q.tasks.async_task"):
        created = client.post(
            "/billing/v2/group-sales/manual/",
            json=_manual_payload(
                context=context,
                offer_digest=offer["offer_digest"],
                idempotency_key="v2-group-manual-k1",
            ),
            **make_auth_params(owner_user, club),
        )

    assert created.status_code == 201, created.json()
    assert created.json()["workspace_state"] == "student"
    assert created.json()["finance_state"] == "pending_manual"
    context["student"].refresh_from_db()
    assert context["student"].status == Student.Status.ACTIVE
    assert Payment.objects.for_club(club).count() == 1


@pytest.mark.django_db
def test_v2_group_offer_and_manual_admission_accept_a_trial_state_lead(
    club,
    owner_user,
    settings,
):
    _enable_v2_group_sales(club=club, settings=settings)
    context = _group_sale_context(club=club, student_status=Student.Status.TRIAL)

    preview = _preview(club=club, owner_user=owner_user, context=context)

    assert preview.status_code == 200, preview.json()
    with patch("django_q.tasks.async_task"):
        created = client.post(
            "/billing/v2/group-sales/manual/",
            json=_manual_payload(
                context=context,
                offer_digest=preview.json()["offer_digest"],
                idempotency_key="v2-group-trial-lead-k1",
            ),
            **make_auth_params(owner_user, club),
        )

    assert created.status_code == 201, created.json()
    assert created.json()["workspace_state"] == "student"
    context["student"].refresh_from_db()
    assert context["student"].status == Student.Status.ACTIVE


@pytest.mark.django_db
def test_v2_group_offer_stale_or_disabled_command_has_no_financial_artifacts(club, owner_user, settings):
    _enable_v2_group_sales(club=club, settings=settings)
    context = _group_sale_context(club=club)
    preview = _preview(club=club, owner_user=owner_user, context=context)
    assert preview.status_code == 200, preview.json()

    context["tariff"].price = 6600
    context["tariff"].save(update_fields=["price", "updated_at"])
    stale = client.post(
        "/billing/v2/group-sales/manual/",
        json={
            "protocol_version": "v2",
            "student_id": context["student"].id,
            "tariff_id": context["tariff"].id,
            "payment_method": "cash",
            "target_training_group_id": context["group"].id,
            "target_schedule_id": context["schedule"].id,
            "target_start_date": context["start_date"].isoformat(),
            "expected_offer_digest": preview.json()["offer_digest"],
            "idempotency_key": "v2-group-stale-k1",
        },
        **make_auth_params(owner_user, club),
    )

    assert stale.status_code == 400, stale.json()
    assert stale.json()["code"] == "group_offer_changed"
    assert Payment.objects.for_club(club).count() == 0
    context["student"].refresh_from_db()
    assert context["student"].status == Student.Status.LEAD


@pytest.mark.django_db
def test_v2_group_command_rejects_client_amount_discount_debt_and_renewal_authority(
    club,
    owner_user,
    settings,
):
    _enable_v2_group_sales(club=club, settings=settings)
    context = _group_sale_context(club=club)
    preview = _preview(club=club, owner_user=owner_user, context=context)
    assert preview.status_code == 200, preview.json()

    rejected = client.post(
        "/billing/v2/group-sales/manual/",
        json={
            **_manual_payload(
                context=context,
                offer_digest=preview.json()["offer_digest"],
                idempotency_key="v2-group-extra-fields-k1",
            ),
            "amount": "1.00",
            "discount_ids": [],
            "debt_ids": [],
            "renewed_from_subscription_id": 1,
        },
        **make_auth_params(owner_user, club),
    )

    assert rejected.status_code == 422, rejected.json()
    assert Payment.objects.for_club(club).count() == 0


@pytest.mark.django_db
def test_v2_group_sale_replays_k1_after_capability_flip_but_rejects_new_k2(
    club,
    owner_user,
    settings,
):
    _enable_v2_group_sales(club=club, settings=settings)
    context = _group_sale_context(club=club)
    preview = _preview(club=club, owner_user=owner_user, context=context)
    assert preview.status_code == 200, preview.json()
    payload = _manual_payload(
        context=context,
        offer_digest=preview.json()["offer_digest"],
        idempotency_key="v2-group-replay-k1",
    )
    with patch("django_q.tasks.async_task"):
        created = client.post(
            "/billing/v2/group-sales/manual/",
            json=payload,
            **make_auth_params(owner_user, club),
        )
    assert created.status_code == 201, created.json()
    settings.UNIFIED_CLIENT_JOURNEY_ENABLED = False

    replay = client.post(
        "/billing/v2/group-sales/manual/",
        json=payload,
        **make_auth_params(owner_user, club),
    )
    assert replay.status_code == 200, replay.json()
    assert replay.json()["command_replayed"] is True
    assert replay.json()["workspace_state"] == "student"

    denied = client.post(
        "/billing/v2/group-sales/manual/",
        json={**payload, "idempotency_key": "v2-group-replay-k2"},
        **make_auth_params(owner_user, club),
    )
    assert denied.status_code == 400, denied.json()
    assert denied.json()["code"] == "commercial_journey_unavailable"
    assert Payment.objects.for_club(club).count() == 1


@pytest.mark.django_db
def test_v2_group_offer_projects_and_validates_fiscal_email_before_sbp_artifacts(
    club,
    owner_user,
    settings,
):
    _enable_v2_group_sales(club=club, settings=settings)
    settings.PAYMENT_PROVIDER = "tochka"
    settings.TOCHKA_RECEIPT_MODE = "tochka_receipt"
    context = _group_sale_context(club=club)
    preview = _preview(club=club, owner_user=owner_user, context=context)
    assert preview.status_code == 200, preview.json()
    offer = preview.json()
    assert offer["buyer_email_required"] is True
    payload = {
        "protocol_version": "v2",
        "student_id": context["student"].id,
        "tariff_id": context["tariff"].id,
        "target_training_group_id": context["group"].id,
        "target_schedule_id": context["schedule"].id,
        "target_start_date": context["start_date"].isoformat(),
        "expected_offer_digest": offer["offer_digest"],
        "idempotency_key": "v2-fiscal-k1",
    }
    missing_email = client.post(
        "/billing/v2/group-sales/bank-orders/",
        json=payload,
        **make_auth_params(owner_user, club),
    )
    assert missing_email.status_code == 400, missing_email.json()
    assert missing_email.json()["code"] == "receipt_buyer_email_required"
    invalid_email = client.post(
        "/billing/v2/group-sales/bank-orders/",
        json={**payload, "idempotency_key": "v2-fiscal-k2", "buyer_email": "not-an-email"},
        **make_auth_params(owner_user, club),
    )
    assert invalid_email.status_code == 400, invalid_email.json()
    assert invalid_email.json()["code"] == "receipt_buyer_email_invalid"
    assert Payment.objects.for_club(club).count() == 0


@pytest.mark.django_db
def test_v2_group_sbp_returns_provider_pending_and_keeps_the_lead_until_confirmation(
    club,
    owner_user,
    settings,
):
    _enable_v2_group_sales(club=club, settings=settings)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
    settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
    context = _group_sale_context(club=club)
    preview = _preview(club=club, owner_user=owner_user, context=context)
    assert preview.status_code == 200, preview.json()

    created = client.post(
        "/billing/v2/group-sales/bank-orders/",
        json={
            "protocol_version": "v2",
            "student_id": context["student"].id,
            "tariff_id": context["tariff"].id,
            "target_training_group_id": context["group"].id,
            "target_schedule_id": context["schedule"].id,
            "target_start_date": context["start_date"].isoformat(),
            "expected_offer_digest": preview.json()["offer_digest"],
            "idempotency_key": "v2-group-sbp-k1",
        },
        **make_auth_params(owner_user, club),
    )

    assert created.status_code == 201, created.json()
    assert created.json()["workspace_state"] == "lead"
    assert created.json()["finance_state"] == "provider_pending"
    assert created.json()["bank_payment_order_id"]
    assert created.json()["provider_payment_url"]
    context["student"].refresh_from_db()
    assert context["student"].status == Student.Status.LEAD
    assert Payment.objects.for_club(club).count() == 1


@pytest.mark.django_db(transaction=True)
def test_v2_group_sbp_commits_claim_before_provider_dispatch(
    club,
    owner_user,
    settings,
):
    _enable_v2_group_sales(club=club, settings=settings)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
    settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
    context = _group_sale_context(club=club)
    offer = _preview(club=club, owner_user=owner_user, context=context).json()
    original = MockPaymentProvider.create_payment_link

    def assert_committed(provider, *, order):
        assert connection.in_atomic_block is False
        assert BankPaymentOrder.objects.for_club(club).filter(id=order.id).exists()
        return original(provider, order=order)

    with patch.object(MockPaymentProvider, "create_payment_link", new=assert_committed):
        created = client.post(
            "/billing/v2/group-sales/bank-orders/",
            json={
                "protocol_version": "v2",
                "student_id": context["student"].id,
                "tariff_id": context["tariff"].id,
                "target_training_group_id": context["group"].id,
                "target_schedule_id": context["schedule"].id,
                "target_start_date": context["start_date"].isoformat(),
                "expected_offer_digest": offer["offer_digest"],
                "idempotency_key": "v2-group-sbp-commit-before-dispatch",
            },
            **make_auth_params(owner_user, club),
        )

    assert created.status_code == 201, created.json()


@pytest.mark.django_db
def test_v2_group_sbp_rejects_distinct_key_for_live_family_and_email_change_on_same_key(
    club,
    owner_user,
    settings,
):
    _enable_v2_group_sales(club=club, settings=settings)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
    settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
    context = _group_sale_context(club=club)
    offer = _preview(club=club, owner_user=owner_user, context=context).json()
    payload = {
        "protocol_version": "v2",
        "student_id": context["student"].id,
        "tariff_id": context["tariff"].id,
        "target_training_group_id": context["group"].id,
        "target_schedule_id": context["schedule"].id,
        "target_start_date": context["start_date"].isoformat(),
        "expected_offer_digest": offer["offer_digest"],
        "idempotency_key": "v2-group-sbp-live-k1",
        "buyer_email": "first@example.test",
    }
    created = client.post(
        "/billing/v2/group-sales/bank-orders/",
        json=payload,
        **make_auth_params(owner_user, club),
    )
    assert created.status_code == 201, created.json()

    changed_email = client.post(
        "/billing/v2/group-sales/bank-orders/",
        json={**payload, "buyer_email": "second@example.test"},
        **make_auth_params(owner_user, club),
    )
    assert changed_email.status_code == 400
    assert changed_email.json()["code"] == "idempotency_conflict"

    distinct_key = client.post(
        "/billing/v2/group-sales/bank-orders/",
        json={**payload, "idempotency_key": "v2-group-sbp-live-k2"},
        **make_auth_params(owner_user, club),
    )
    assert distinct_key.status_code == 400
    assert distinct_key.json()["code"] == "bank_payment_order_live_command_conflict"
    assert Payment.objects.for_club(club).count() == 1
    assert BankPaymentOrder.objects.for_club(club).count() == 1


@pytest.mark.django_db
def test_v2_group_sbp_replay_returns_current_receipt_before_and_after_provider_confirmation(
    club,
    owner_user,
    settings,
):
    _enable_v2_group_sales(club=club, settings=settings)
    settings.PAYMENT_PROVIDER = BankPaymentOrder.Provider.MOCK
    settings.ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
    settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
    settings.MOCK_PAYMENT_WEBHOOKS_ENABLED = True
    context = _group_sale_context(club=club)
    preview = _preview(club=club, owner_user=owner_user, context=context)
    assert preview.status_code == 200, preview.json()
    payload = {
        "protocol_version": "v2",
        "student_id": context["student"].id,
        "tariff_id": context["tariff"].id,
        "target_training_group_id": context["group"].id,
        "target_schedule_id": context["schedule"].id,
        "target_start_date": context["start_date"].isoformat(),
        "expected_offer_digest": preview.json()["offer_digest"],
        "idempotency_key": "v2-group-sbp-replay-k1",
    }
    created = client.post(
        "/billing/v2/group-sales/bank-orders/",
        json=payload,
        **make_auth_params(owner_user, club),
    )
    assert created.status_code == 201, created.json()

    replay_pending = client.post(
        "/billing/v2/group-sales/bank-orders/",
        json=payload,
        **make_auth_params(owner_user, club),
    )
    assert replay_pending.status_code == 200, replay_pending.json()
    assert replay_pending.json()["command_replayed"] is True
    assert replay_pending.json()["workspace_state"] == "lead"
    assert replay_pending.json()["finance_state"] == "provider_pending"
    assert replay_pending.json()["payment_id"] == created.json()["payment_id"]
    assert replay_pending.json()["bank_payment_order_id"] == created.json()["bank_payment_order_id"]
    assert Payment.objects.for_club(club).count() == 1
    assert BankPaymentOrder.objects.for_club(club).count() == 1

    order = BankPaymentOrder.objects.for_club(club).get(id=created.json()["bank_payment_order_id"])
    webhook_body = json.dumps(
        {
            "webhookType": "acquiringInternetPayment",
            "event_id": f"v2-replay-confirm-{order.id}",
            "status": "APPROVED",
            "paymentLinkId": order.provider_payment_link_id,
            "operationId": f"v2-replay-operation-{order.id}",
            "amount": str(order.amount_snapshot),
            "paid_at": timezone.now().isoformat(),
        }
    ).encode()
    provider_event = process_bank_payment_webhook(
        provider=BankPaymentOrder.Provider.MOCK,
        request_body=webhook_body,
        headers={},
        request_id="v2-group-sbp-replay-confirm",
    )
    order.refresh_from_db()
    order.payment.refresh_from_db()
    assert provider_event.order_id == order.id
    assert order.status == BankPaymentOrder.Status.APPROVED
    assert order.payment.status == Payment.Status.CONFIRMED

    replay_confirmed = client.post(
        "/billing/v2/group-sales/bank-orders/",
        json=payload,
        **make_auth_params(owner_user, club),
    )
    assert replay_confirmed.status_code == 200, replay_confirmed.json()
    assert replay_confirmed.json()["command_replayed"] is True
    assert replay_confirmed.json()["workspace_state"] == "student"
    assert replay_confirmed.json()["finance_state"] == "confirmed"
    assert replay_confirmed.json()["payment_id"] == created.json()["payment_id"]
    assert replay_confirmed.json()["bank_payment_order_id"] == created.json()["bank_payment_order_id"]
    assert Payment.objects.for_club(club).count() == 1
    assert BankPaymentOrder.objects.for_club(club).count() == 1


@pytest.mark.django_db
def test_latest_trial_recommendation_uses_only_compatible_canonical_evidence(
    club,
):
    today = timezone.localdate()
    training_type = TrainingTypeFactory(club=club, kind="group")
    location = LocationFactory(club=club)
    trainer = TrainerFactory(club=club)
    student = StudentFactory(club=club, status=Student.Status.LEAD, lead_status=Student.LeadStatus.NEW)
    first_group = TrainingGroup.objects.create(
        club=club,
        name="Checked-in trial",
        training_type=training_type,
        location=location,
        responsible_trainer=trainer,
        status=TrainingGroup.Status.ACTIVE,
    )
    second_group = TrainingGroup.objects.create(
        club=club,
        name="Older booking timestamp",
        training_type=training_type,
        location=location,
        responsible_trainer=trainer,
        status=TrainingGroup.Status.ACTIVE,
    )
    first_schedule = ScheduleFactory(
        club=club,
        training_group=first_group,
        trainer=trainer,
        location=location,
        training_type=training_type,
        day_of_week=today.weekday(),
    )
    second_schedule = ScheduleFactory(
        club=club,
        training_group=second_group,
        trainer=trainer,
        location=location,
        training_type=training_type,
        day_of_week=today.weekday(),
    )
    ScheduleEnrollment.objects.create(
        club=club,
        student=student,
        schedule=first_schedule,
        status=ScheduleEnrollment.Status.TRIAL,
        starts_on=today - timedelta(days=3),
        ends_on=today - timedelta(days=1),
        trial_at=timezone.now() - timedelta(days=3),
        created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
    )
    ScheduleEnrollment.objects.create(
        club=club,
        student=student,
        schedule=second_schedule,
        status=ScheduleEnrollment.Status.TRIAL,
        starts_on=today - timedelta(days=2),
        ends_on=today - timedelta(days=2),
        trial_at=timezone.now() - timedelta(days=2),
        created_from=ScheduleEnrollment.CreatedFrom.LEAD_BOOKING,
    )
    CheckinFactory(
        club=club,
        student=student,
        schedule=first_schedule,
        training_type=training_type,
        date=today - timedelta(days=1),
    )
    tariff = TariffFactory(club=club, training_type=training_type, location=location)

    cards = get_group_enrollment_options(
        club=club,
        student_id=student.id,
        tariff_id=tariff.id,
        canonical_cards_enabled=True,
    )

    assert cards[0]["training_group_id"] == first_group.id
    assert cards[0]["is_latest_trial_group"] is True
    assert [card for card in cards if card["is_latest_trial_group"]] == [cards[0]]
