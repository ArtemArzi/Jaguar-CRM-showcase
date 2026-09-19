from __future__ import annotations

import io
import json
import uuid
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import override_settings

from apps.attendance.models import TrainingGroupMembership, TrainingGroupRolloutState
from apps.billing.models import Subscription
from apps.clubs.models import Club, ClubSettings
from apps.common.management.commands.assert_unified_client_commercial_journey_e2e import Command
from apps.common.management.commands.prepare_unified_client_commercial_journey_e2e import (
    Command as PrepareUnifiedClientCommercialJourneyCommand,
)
from apps.students.models import Student


def _fixture(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _uuid_with_prefix(prefix: str) -> uuid.UUID:
    return uuid.UUID(hex=f"{prefix}{'0' * 24}")


@pytest.mark.django_db
@override_settings(
    TRAINING_GROUP_NEW_WRITES_ENABLED=True,
    MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
    PAYMENT_PROVIDER="mock",
    ONLINE_PAYMENT_ORDER_CREATION_ENABLED=True,
    MOCK_PAYMENT_ORDER_CREATION_ENABLED=True,
    MOCK_PAYMENT_WEBHOOKS_ENABLED=True,
    JAGUAR_PAYMENT_RETURN_ORIGIN="http://127.0.0.1:4174",
)
def test_prepare_unified_client_commercial_journey_e2e_builds_canonical_slice6_fixture(tmp_path):
    output = tmp_path / "fixture.json"
    stdout = io.StringIO()

    call_command("prepare_unified_client_commercial_journey_e2e", output=str(output), stdout=stdout)

    fixture = _fixture(output)
    commercial = fixture["commercial"]
    settings_row = ClubSettings.objects.get(club_id=fixture["club_id"])
    source = Subscription.objects.get(id=commercial["renewal_source_subscription_id"])
    membership = TrainingGroupMembership.objects.get(id=commercial["renewal_membership_id"])

    assert settings_row.unified_client_journey_enabled is True
    assert settings_row.commercial_journey_protocol_version == "v2"
    assert TrainingGroupRolloutState.objects.get(club_id=fixture["club_id"]).mode == "active"
    assert commercial["protocol_version"] == "v2"
    assert commercial["training_group_id"]
    assert commercial["training_type_id"]
    assert commercial["schedule_id"]
    assert commercial["start_date"]
    assert commercial["tariff_price"] == "5000.00"
    assert commercial["tariff_trainings_limit"] == 8
    assert commercial["tariff_duration_days"] == 30
    assert source.status == Subscription.Status.ACTIVE
    assert source.trainings_left == 0
    assert membership.training_group_id == commercial["training_group_id"]
    assert membership.student_id == commercial["renewal_student_id"]
    assert fixture["cash_group_lead"]["id"] != commercial["renewal_student_id"]
    assert fixture["sbp_group_lead"]["id"] not in {
        fixture["cash_group_lead"]["id"],
        commercial["renewal_student_id"],
    }
    assert "password" not in stdout.getvalue().lower()

    second_output = tmp_path / "fixture-second.json"
    call_command(
        "prepare_unified_client_commercial_journey_e2e",
        output=str(second_output),
        stdout=io.StringIO(),
    )
    second_fixture = _fixture(second_output)
    first_phones = set(
        Student.objects.for_club(fixture["club_id"]).values_list("phone", flat=True)
    )
    second_phones = set(
        Student.objects.for_club(second_fixture["club_id"]).values_list("phone", flat=True)
    )
    assert first_phones.isdisjoint(second_phones)


@pytest.mark.django_db
def test_prepare_unified_client_commercial_journey_e2e_retries_phone_block_collisions():
    collision_club = Club.objects.create(
        name="Unified fixture phone collision",
        city="E2E",
        disciplines=["muay_thai"],
        timezone="Europe/Moscow",
    )
    Student.objects.create(
        club=collision_club,
        first_name="ExistingPhone",
        phone="+79000000102",
        lead_status=Student.LeadStatus.NEW,
    )
    get_user_model().objects.create_user(username="+79000000210")
    candidate_uuids = iter(
        [
            _uuid_with_prefix("00000001"),
            _uuid_with_prefix("00989681"),
            _uuid_with_prefix("00000002"),
            _uuid_with_prefix("00000003"),
        ]
    )
    command = PrepareUnifiedClientCommercialJourneyCommand()

    with patch(
        "apps.common.management.commands.prepare_unified_client_commercial_journey_e2e.uuid.uuid4",
        side_effect=lambda: next(candidate_uuids),
    ) as mock_uuid4:
        unique, fixture_phones = command._reserve_fixture_phone_block()

    assert unique == "00000003"
    assert mock_uuid4.call_count == 4
    assert fixture_phones == {index: f"+790000003{index:02d}" for index in range(1, 11)}


@pytest.mark.django_db
def test_prepare_unified_client_commercial_journey_e2e_fails_closed_when_phone_blocks_exhaust():
    get_user_model().objects.create_user(username="+79000000110")
    command = PrepareUnifiedClientCommercialJourneyCommand()

    with patch(
        "apps.common.management.commands.prepare_unified_client_commercial_journey_e2e.uuid.uuid4",
        return_value=_uuid_with_prefix("00000001"),
    ) as mock_uuid4:
        with pytest.raises(CommandError, match="Unable to reserve an isolated fixture phone block"):
            command._reserve_fixture_phone_block()

    assert mock_uuid4.call_count == command.MAX_FIXTURE_PHONE_BLOCK_ATTEMPTS


@pytest.mark.django_db
@override_settings(
    TRAINING_GROUP_NEW_WRITES_ENABLED=True,
    MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
    PAYMENT_PROVIDER="mock",
    ONLINE_PAYMENT_ORDER_CREATION_ENABLED=True,
    MOCK_PAYMENT_ORDER_CREATION_ENABLED=True,
    MOCK_PAYMENT_WEBHOOKS_ENABLED=True,
    JAGUAR_PAYMENT_RETURN_ORIGIN="http://127.0.0.1:4174",
)
def test_assert_unified_client_commercial_journey_e2e_requires_browser_group_receipt(tmp_path):
    output = tmp_path / "fixture.json"
    call_command("prepare_unified_client_commercial_journey_e2e", output=str(output), stdout=io.StringIO())

    with pytest.raises(CommandError, match="contact outcome|browser manual group payment"):
        call_command(
            "assert_unified_client_commercial_journey_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )


@pytest.mark.django_db
@override_settings(
    TRAINING_GROUP_NEW_WRITES_ENABLED=True,
    MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
    PAYMENT_PROVIDER="mock",
    ONLINE_PAYMENT_ORDER_CREATION_ENABLED=True,
    MOCK_PAYMENT_ORDER_CREATION_ENABLED=True,
    MOCK_PAYMENT_WEBHOOKS_ENABLED=True,
    JAGUAR_PAYMENT_RETURN_ORIGIN="http://127.0.0.1:4174",
)
def test_assert_unified_client_commercial_journey_e2e_rejects_missing_v2_sbp_fixture_context(tmp_path):
    output = tmp_path / "fixture.json"
    call_command("prepare_unified_client_commercial_journey_e2e", output=str(output), stdout=io.StringIO())
    fixture = _fixture(output)
    fixture.pop("sbp_group_lead")
    output.write_text(json.dumps(fixture), encoding="utf-8")

    with pytest.raises(CommandError, match="sbp_group_lead"):
        call_command(
            "assert_unified_client_commercial_journey_e2e",
            fixture=str(output),
            timeout_seconds=0,
            stdout=io.StringIO(),
        )


@pytest.mark.django_db
@override_settings(
    UNIFIED_CLIENT_JOURNEY_ENABLED=True,
    TRAINING_GROUP_NEW_WRITES_ENABLED=True,
    MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
    PAYMENT_PROVIDER="mock",
    ONLINE_PAYMENT_ORDER_CREATION_ENABLED=True,
    MOCK_PAYMENT_ORDER_CREATION_ENABLED=True,
    MOCK_PAYMENT_WEBHOOKS_ENABLED=True,
    JAGUAR_PAYMENT_RETURN_ORIGIN="http://127.0.0.1:4174",
)
def test_assert_unified_client_commercial_journey_e2e_refreshes_new_manual_group_admissions(tmp_path):
    output = tmp_path / "fixture.json"
    call_command("prepare_unified_client_commercial_journey_e2e", output=str(output), stdout=io.StringIO())

    fixture = _fixture(output)
    club = Club.objects.get(id=fixture["club_id"])
    command = Command()
    checkin_lead = Student.objects.for_club(club).get(id=fixture["checkin_group_lead"]["id"])
    reject_lead = Student.objects.for_club(club).get(id=fixture["reject_group_lead"]["id"])

    with (
        patch("apps.billing.service_modules.sale_earnings._enqueue_sale_earning_after_commit"),
        patch("django_q.tasks.async_task"),
    ):
        command._create_pending_group_payment(
            club=club,
            student_id=checkin_lead.id,
            commercial=fixture["commercial"],
            key=f"{fixture['fixture_id']}-refresh-checkin",
            recorded_by_id=fixture["trainer"]["user_id"],
        )
        command._create_pending_group_payment(
            club=club,
            student_id=reject_lead.id,
            commercial=fixture["commercial"],
            key=f"{fixture['fixture_id']}-refresh-reject",
            recorded_by_id=fixture["trainer"]["user_id"],
        )

    assert checkin_lead.status == Student.Status.LEAD
    assert reject_lead.status == Student.Status.LEAD

    command._assert_v2_manual_group_admissions(checkin_lead, reject_lead)

    assert checkin_lead.status == Student.Status.ACTIVE
    assert reject_lead.status == Student.Status.ACTIVE


@pytest.mark.django_db
@override_settings(
    TRAINING_GROUP_NEW_WRITES_ENABLED=True,
    MANUAL_OPERATIONAL_ADMISSION_ENABLED=True,
)
def test_assert_unified_client_commercial_journey_e2e_retries_with_a_rolled_back_attempt(tmp_path):
    output = tmp_path / "fixture.json"
    call_command("prepare_unified_client_commercial_journey_e2e", output=str(output), stdout=io.StringIO())
    fixture = _fixture(output)
    student = Student.objects.get(id=fixture["checkin_group_lead"]["id"])
    attempts = 0

    def collect_evidence(_command, _fixture):
        nonlocal attempts

        attempts += 1
        if attempts == 1:
            student.status = Student.Status.ACTIVE
            student.save(update_fields=["status"])
            raise CommandError("temporary evidence failure")

        student.refresh_from_db()
        assert student.status == Student.Status.LEAD
        return {"ok": True}

    with (
        patch.object(Command, "_load_fixture", autospec=True, return_value={}),
        patch.object(Command, "_collect_evidence", autospec=True, side_effect=collect_evidence),
        patch(
            "apps.common.management.commands.assert_unified_client_commercial_journey_e2e.time.monotonic",
            return_value=0,
        ),
        patch("apps.common.management.commands.assert_unified_client_commercial_journey_e2e.time.sleep"),
    ):
        call_command(
            "assert_unified_client_commercial_journey_e2e",
            fixture=str(output),
            timeout_seconds=1,
            stdout=io.StringIO(),
        )

    student.refresh_from_db()
    assert attempts == 2
    assert student.status == Student.Status.LEAD
