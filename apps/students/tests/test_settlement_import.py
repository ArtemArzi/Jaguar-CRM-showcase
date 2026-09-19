from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

import pytest

from apps.billing.tests.test_opening_issuer import command as command
from apps.billing.tests.test_opening_issuer import group_command as group_command
from apps.clubs.timezones import club_localdate
from apps.students.imports.parsing import SETTLEMENTS_SHEET, WorkbookRow
from apps.students.imports.runner import accept_batch, run_batch_chunk
from apps.students.imports.services import prepare_batch, validate_batch
from apps.students.imports.workbooks import workbook_bytes
from apps.students.models import OpeningImportItemReceipt
from apps.trainers.models import TrainerSettlementEntry
from apps.trainers.tests.factories import TrainerFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def settlement_book(club, owner_user, settings, tmp_path):
    settings.TRAINER_SETTLEMENTS_ENABLED = settings.STUDENT_OPENING_IMPORT_ENABLED = True
    settings.STUDENT_IMPORT_PRIVATE_ROOT = tmp_path / "private"
    trainer = TrainerFactory(club=club)
    day = club_localdate(club) - timedelta(days=1)
    rows = [
        {
            "source_key": "opening",
            "Вид записи": "Начальный расчёт",
            "ID тренера": trainer.id,
            "Дата": str(day),
            "Сумма": "1000",
            "Причина": "Сверено",
            "Подтверждено": True,
        },
        {
            "source_key": "payout",
            "Вид записи": "Выплата",
            "ID тренера": trainer.id,
            "Дата": str(day),
            "Сумма": "300",
            "Причина": "Выдано",
            "Подтверждено": True,
            "Способ выплаты": "Наличные",
        },
    ]
    return trainer, rows


def prepare(club, owner, rows, tmp_path):
    path = tmp_path / "settlements.xlsx"
    path.write_bytes(
        workbook_bytes(
            club_id=club.id,
            actor_user_id=owner.id,
            namespace="settlement-fixture",
            rows=[WorkbookRow(SETTLEMENTS_SHEET, i, row) for i, row in enumerate(rows, 2)],
        )
    )
    return prepare_batch(club_id=club.id, actor_user_id=owner.id, path=path)


def accept(club, owner, batch):
    preview = validate_batch(club_id=club.id, actor_user_id=owner.id, batch_id=batch.id)
    assert preview.selection, list(batch.items.values("errors"))
    accept_batch(
        club_id=club.id,
        actor_user_id=owner.id,
        batch_id=batch.id,
        revision=preview.revision,
        selection_token=str(preview.selection_token),
        channel="assistant_cli",
    )
    return preview


def test_settlement_book_orders_baseline_before_payout_and_replays(
    club, owner_user, settlement_book, tmp_path, settings
):
    trainer, rows = settlement_book
    batch = prepare(club, owner_user, list(reversed(rows)), tmp_path)
    preview = accept(club, owner_user, batch)
    assert [entry["kind"] for entry in preview.selection] == ["settlement_opening", "settlement_payout"]
    assert not TrainerSettlementEntry.objects.exists()
    assert run_batch_chunk(club_id=club.id, batch_id=batch.id)["counts"] == {"applied": 2}
    assert TrainerSettlementEntry.objects.get(kind="payout").balance_delta == Decimal("-300")
    assert OpeningImportItemReceipt.objects.count() == 2
    settings.TRAINER_SETTLEMENTS_ENABLED = settings.STUDENT_OPENING_IMPORT_ENABLED = False
    second = prepare(club, owner_user, rows, tmp_path)
    accept(club, owner_user, second)
    assert run_batch_chunk(club_id=club.id, batch_id=second.id)["counts"] == {"replayed": 2}
    assert TrainerSettlementEntry.objects.count() == 2


def test_failed_baseline_blocks_dependent_payout_without_partial_receipt(
    club, owner_user, settlement_book, tmp_path, settings
):
    _, rows = settlement_book
    batch = prepare(club, owner_user, rows, tmp_path)
    accept(club, owner_user, batch)
    settings.TRAINER_SETTLEMENTS_ENABLED = False
    result = run_batch_chunk(club_id=club.id, batch_id=batch.id)
    assert result["status"] == "partial" and result["counts"] == {"needs_review": 2}
    assert not TrainerSettlementEntry.objects.exists() and not OpeningImportItemReceipt.objects.exists()
    assert batch.items.get(kind="settlement_payout").errors[0]["code"] == "import_dependency_unresolved"


def test_lost_response_after_first_item_resumes_exactly_once(club, owner_user, settlement_book, tmp_path):
    _, rows = settlement_book
    batch = prepare(club, owner_user, rows, tmp_path)
    accept(club, owner_user, batch)
    from apps.students.imports.runner import _apply_item

    calls = []

    def interrupted(**kwargs):
        if calls:
            raise RuntimeError("synthetic connection loss")
        _apply_item(**kwargs)
        calls.append(1)

    with patch("apps.students.imports.runner._apply_item", side_effect=interrupted):
        with pytest.raises(RuntimeError):
            run_batch_chunk(club_id=club.id, batch_id=batch.id)
    assert TrainerSettlementEntry.objects.count() == 1
    assert run_batch_chunk(club_id=club.id, batch_id=batch.id)["counts"] == {"applied": 2}
    assert TrainerSettlementEntry.objects.count() == 2


def test_changed_source_key_payload_and_duplicate_openings_need_review(club, owner_user, settlement_book, tmp_path):
    _, rows = settlement_book
    batch = prepare(club, owner_user, rows, tmp_path)
    accept(club, owner_user, batch)
    run_batch_chunk(club_id=club.id, batch_id=batch.id)
    altered = [{**rows[0], "Сумма": "1100"}, rows[1]]
    second = prepare(club, owner_user, altered, tmp_path)
    preview = validate_batch(club_id=club.id, actor_user_id=owner_user.id, batch_id=second.id)
    assert len(preview.selection) == 1
    assert second.items.get(kind="settlement_opening").errors[0]["code"] == "idempotency_conflict"
    assert TrainerSettlementEntry.objects.count() == 2


def test_unconfirmed_or_blank_initial_amount_never_becomes_zero(club, owner_user, settlement_book, tmp_path):
    _, rows = settlement_book
    for change in ({"Сумма": None}, {"Подтверждено": False}):
        batch = prepare(club, owner_user, [{**rows[0], **change}], tmp_path)
        assert not validate_batch(club_id=club.id, actor_user_id=owner_user.id, batch_id=batch.id).selection
    assert not TrainerSettlementEntry.objects.exists()


def test_external_earned_change_invalidates_baseline_preview(club, owner_user, settlement_book, tmp_path):
    from apps.attendance.tests.factories import CheckinFactory
    from apps.trainers.tests.factories import TrainerEarningFactory

    trainer, rows = settlement_book
    batch = prepare(club, owner_user, rows, tmp_path)
    accept(club, owner_user, batch)
    visit = CheckinFactory(club=club, trainer=trainer, date=club_localdate(club) - timedelta(days=3))
    TrainerEarningFactory(club=club, trainer=trainer, checkin=visit)
    result = run_batch_chunk(club_id=club.id, batch_id=batch.id)
    assert result["counts"] == {"needs_review": 2}
    assert batch.items.get(kind="settlement_opening").errors[0]["code"] == "import_preview_stale"
    assert not TrainerSettlementEntry.objects.exists()


@pytest.mark.parametrize("kind", ["personal", "group"])
def test_mixed_entitlement_baseline_payout_dependencies(club, request, tmp_path, settings, kind):
    from apps.billing.models import OpeningEntitlementSnapshot
    from apps.students.imports.parsing import ENTITLEMENTS_SHEET
    from apps.students.imports.schemas import COLUMNS
    from apps.trainers.settlement_selectors import get_trainer_settlement_summary

    prepared = request.getfixturevalue("group_command" if kind == "group" else "command")
    actor, terms = prepared[:2]
    trainer_id = terms.sale_trainer_id if kind == "group" else terms.package_owner_trainer_id
    settings.TRAINER_SETTLEMENTS_ENABLED = settings.STUDENT_OPENING_IMPORT_ENABLED = True
    settings.STUDENT_IMPORT_PRIVATE_ROOT = tmp_path / "private"
    payload = terms.as_payload()
    values = {column: payload[field] for field, column in COLUMNS.items()}
    values.update(
        {
            "Исходная цена": payload["paid_amount"],
            "Валюта": "RUB",
            "Исторический долг": 0,
            "Заморожен": False,
            "Тип пакета": "Групповой" if kind == "group" else "Персональный",
        }
    )
    day = str(club_localdate(club))
    opening = {
        "source_key": "opening",
        "Вид записи": "Начальный расчёт",
        "ID тренера": trainer_id,
        "Дата": day,
        "Сумма": "1000",
        "Причина": "Сверено",
        "Подтверждено": True,
    }
    payout = {**opening, "source_key": "paid", "Вид записи": "Выплата", "Сумма": "300", "Способ выплаты": "Наличные"}
    path = tmp_path / "mixed.xlsx"
    path.write_bytes(
        workbook_bytes(
            club_id=club.id,
            actor_user_id=actor.id,
            namespace=terms.source_namespace,
            rows=[
                WorkbookRow(ENTITLEMENTS_SHEET, 2, values),
                WorkbookRow(SETTLEMENTS_SHEET, 2, payout),
                WorkbookRow(SETTLEMENTS_SHEET, 3, opening),
            ],
        )
    )
    batch = prepare_batch(club_id=club.id, actor_user_id=actor.id, path=path)
    preview = accept(club, actor, batch)
    assert [entry["kind"] for entry in preview.selection] == ["entitlement", "settlement_opening", "settlement_payout"]
    assert run_batch_chunk(club_id=club.id, batch_id=batch.id)["counts"] == {"applied": 3}
    assert OpeningEntitlementSnapshot.objects.count() == 1
    assert (
        get_trainer_settlement_summary(
            club=club,
            trainer_id=trainer_id,
            date_from=club_localdate(club),
            date_to=club_localdate(club),
        )["balance"]
        == 700
    )
