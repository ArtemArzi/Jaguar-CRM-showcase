import io
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

from apps.attendance.models import PersonalAvailabilitySlot
from apps.billing.models import Tariff
from apps.clubs.models import ClubSettings
from apps.common.management.commands.disable_unified_client_journey_e2e import (
    _assert_isolated_e2e_database,
)


def _fixture(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.django_db
def test_prepare_student_parent_self_booking_e2e_keeps_legacy_and_unified_clubs_isolated(tmp_path):
    output = tmp_path / "fixture.json"
    stdout = io.StringIO()

    call_command("prepare_student_parent_self_booking_e2e", output=str(output), stdout=stdout)

    fixture = _fixture(output)
    legacy = fixture["legacy"]
    unified = fixture["unified"]
    legacy_settings = ClubSettings.objects.get(club_id=legacy["club_id"])
    unified_settings = ClubSettings.objects.get(club_id=unified["club_id"])
    unified_tariff = Tariff.objects.get(id=unified["personal_tariff"]["id"])

    assert fixture["fixture_id"]
    assert legacy_settings.unified_client_journey_enabled is False
    assert unified_settings.unified_client_journey_enabled is True
    assert unified_tariff.is_personal_booking_default is True
    assert PersonalAvailabilitySlot.objects.filter(
        id__in=[
            unified["student_slot_id"],
            unified["parent_slot_id"],
            unified["payer_slot_id"],
        ],
        status=PersonalAvailabilitySlot.Status.PUBLISHED,
    ).count() == 3
    assert unified["student"]["student_id"] != unified["payer"]["student_id"]
    assert unified["parent"]["child_id"] != unified["payer"]["student_id"]
    assert "password" not in stdout.getvalue().lower()


@pytest.mark.django_db
def test_disable_unified_client_journey_e2e_is_fixture_scoped_and_idempotent(tmp_path):
    output = tmp_path / "fixture.json"
    call_command(
        "prepare_student_parent_self_booking_e2e",
        output=str(output),
        stdout=io.StringIO(),
    )
    fixture = _fixture(output)
    club_id = fixture["unified"]["club_id"]
    assert ClubSettings.objects.get(club_id=club_id).unified_client_journey_enabled is True

    with patch(
        "apps.common.management.commands.disable_unified_client_journey_e2e._assert_isolated_e2e_database"
    ):
        first_stdout = io.StringIO()
        call_command(
            "disable_unified_client_journey_e2e",
            fixture=str(output),
            stdout=first_stdout,
        )
        second_stdout = io.StringIO()
        call_command(
            "disable_unified_client_journey_e2e",
            fixture=str(output),
            stdout=second_stdout,
        )

    assert json.loads(first_stdout.getvalue()) == {
        "ok": True,
        "club_id": club_id,
        "previous_enabled": True,
        "enabled": False,
    }
    assert json.loads(second_stdout.getvalue()) == {
        "ok": True,
        "club_id": club_id,
        "previous_enabled": False,
        "enabled": False,
    }
    assert ClubSettings.objects.get(club_id=club_id).unified_client_journey_enabled is False


def test_disable_unified_client_journey_e2e_rejects_unvalidated_database(monkeypatch):
    monkeypatch.delenv("REAL_STACK_E2E_DATABASE_URL", raising=False)
    with pytest.raises(CommandError, match="isolated_e2e_database_required"):
        call_command(
            "disable_unified_client_journey_e2e",
            fixture="missing.json",
            stdout=io.StringIO(),
        )


def test_disable_unified_client_journey_e2e_guard_binds_url_to_active_connection():
    active_connection = SimpleNamespace(
        vendor="postgresql",
        connection=SimpleNamespace(
            info=SimpleNamespace(
                dbname="slice8_guard_e2e",
                host="127.0.0.1",
                port=55441,
            )
        ),
        ensure_connection=lambda: None,
    )
    with (
        patch.dict(
            "os.environ",
            {
                "REAL_STACK_E2E_DATABASE_URL": (
                    "postgresql://local-user@127.0.0.1:55441/slice8_guard_e2e"
                )
            },
            clear=False,
        ),
        patch(
            "apps.common.management.commands.disable_unified_client_journey_e2e.connection",
            active_connection,
        ),
    ):
        _assert_isolated_e2e_database()

    for unsafe_url in (
        "postgresql://local-user@127.0.0.1:55442/slice8_guard_e2e",
        "postgresql://local-user@127.0.0.1:55441/slice8_guard_e2e?service=other",
    ):
        with (
            patch.dict("os.environ", {"REAL_STACK_E2E_DATABASE_URL": unsafe_url}, clear=False),
            patch(
                "apps.common.management.commands.disable_unified_client_journey_e2e.connection",
                active_connection,
            ),
            pytest.raises(CommandError, match="isolated_e2e_database_required"),
        ):
            _assert_isolated_e2e_database()
