"""Append-only actual settlement journal, separate from earned salary and P&L."""

import hashlib
import json
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation

from django.conf import settings
from django.db import transaction

from apps.clubs.models import Club, ClubMembership
from apps.clubs.timezones import club_localdate
from apps.common.exceptions import BusinessLogicError
from apps.trainers.models import (
    Trainer,
    TrainerSettlementEntry,
    TrainerSettlementReconciliation,
    TrainerSettlementResolution,
)
from apps.trainers.settlement_selectors import get_trainer_settlement_summary


def _fail(message, code="settlement_needs_review"):
    raise BusinessLogicError(message, code=code)


def _authorize(*, club_id, actor_user_id):
    if not ClubMembership.objects.filter(
        club_id=club_id,
        user_id=actor_user_id,
        user__is_active=True,
        is_active=True,
        role__in=["owner", "admin"],
    ).exists():
        _fail("Действие доступно владельцу или администратору клуба.", "actor_not_authorized")


def _money(value):
    try:
        number = Decimal(str(value))
        if (
            not number.is_finite()
            or abs(number) >= Decimal("1000000000000")
            or number != number.quantize(Decimal("0.01"))
        ):
            raise ValueError
        return number.quantize(Decimal("0.01"))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BusinessLogicError("Укажите сумму с точностью до копейки.", code="settlement_invalid_amount") from exc


def _hash(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def _identity(*, source_namespace, source_key, reason):
    namespace, key, reason = str(source_namespace).strip(), str(source_key).strip(), str(reason).strip()
    if not namespace or len(namespace) > 120 or not key or len(key) > 160:
        _fail("Не задан постоянный ключ записи.", "settlement_invalid_key")
    if not reason or len(reason) > 1000:
        _fail("Укажите причину или основание до 1000 символов.", "settlement_reason_required")
    return namespace, key, reason


def _lock_trainers(*, club_id, trainer_ids):
    trainers = list(
        Trainer.objects.for_club(club_id)
        .select_for_update()
        .filter(
            id__in=sorted(set(trainer_ids)),
        )
        .order_by("id")
    )
    if {t.id for t in trainers} != set(trainer_ids):
        _fail("Тренер недоступен.", "target_not_available")
    return trainers


def record_historical_settlement_change(
    *, club_id: int, trainer_id: int, effective_on: date, event_key: str, suggested_delta: Decimal, evidence: dict
):
    """Called inside each financial writer; same Club fence as baseline/payout.

    Existing writers must not depend on the new-write flag: accepted baselines
    remain protected when new settlement creation is disabled.
    """
    with transaction.atomic():
        Club.objects.select_for_update(no_key=True).get(id=club_id)
        _lock_trainers(club_id=club_id, trainer_ids=[trainer_id])
        opening = (
            TrainerSettlementEntry.objects.for_club(club_id)
            .filter(
                trainer_id=trainer_id,
                kind="opening",
                effective_on__gt=effective_on,
            )
            .first()
        )
        if opening is None:
            return None
        case, _ = TrainerSettlementReconciliation.objects.get_or_create(
            club_id=club_id,
            trainer_id=trainer_id,
            event_key=event_key,
            defaults={
                "opening": opening,
                "effective_on": effective_on,
                "suggested_delta": _money(suggested_delta),
                "evidence": evidence,
            },
        )
        return case


def note_earning_created(*, earning):
    from apps.billing.recognition import payment_recognition_date

    earning.refresh_from_db(fields=["amount"])
    effective_on = earning.checkin.date if earning.checkin_id else payment_recognition_date(payment=earning.payment)
    if effective_on is not None:
        record_historical_settlement_change(
            club_id=earning.club_id,
            trainer_id=earning.trainer_id,
            effective_on=effective_on,
            event_key=f"earning:{earning.id}:created",
            suggested_delta=earning.amount,
            evidence={"earning_id": earning.id, "checkin_id": earning.checkin_id, "payment_id": earning.payment_id},
        )


def note_adjustment_created(*, adjustment):
    if adjustment.affects_payroll and adjustment.payable_amount_delta:
        record_historical_settlement_change(
            club_id=adjustment.club_id,
            trainer_id=adjustment.trainer_id,
            effective_on=adjustment.effective_date,
            event_key=f"adjustment:{adjustment.id}:created",
            suggested_delta=adjustment.payable_amount_delta,
            evidence={"adjustment_id": adjustment.id, "kind": adjustment.kind},
        )


def _summary(*, club, trainer_id, day):
    return get_trainer_settlement_summary(club=club, trainer_id=trainer_id, date_from=day, date_to=day)


def _settlement_command(
    *,
    club_id: int,
    actor_user_id: int,
    trainer_id: int,
    kind: str,
    effective_on: date,
    reason: str,
    source_namespace: str,
    source_key: str,
    balance_delta=None,
    amount=None,
    reversal_of_id=None,
    payment_method="",
    confirm_advance=False,
    expected_fingerprint=None,
    channel="admin",
    apply=True,
):
    namespace, key, reason = _identity(source_namespace=source_namespace, source_key=source_key, reason=reason)
    if kind not in TrainerSettlementEntry.Kind.values or channel not in {"admin", "import", "cli"}:
        _fail("Неизвестный вид записи.", "settlement_invalid_kind")
    if not isinstance(effective_on, date):
        _fail("Укажите дату расчёта.", "settlement_invalid_date")
    signed = _money(balance_delta) if balance_delta is not None else None
    cash = _money(amount) if amount is not None else None
    payload = {
        "trainer_id": trainer_id,
        "kind": kind,
        "effective_on": str(effective_on),
        "reason": reason,
        "balance_delta": str(signed) if signed is not None else None,
        "amount": str(cash) if cash is not None else None,
        "reversal_of_id": reversal_of_id,
        "payment_method": payment_method,
        "confirm_advance": bool(confirm_advance),
    }
    fingerprint = _hash(payload)
    with transaction.atomic():
        club = Club.objects.select_for_update(no_key=True).get(id=club_id)
        _authorize(club_id=club_id, actor_user_id=actor_user_id)
        _lock_trainers(club_id=club_id, trainer_ids=[trainer_id])
        entries = TrainerSettlementEntry.objects.for_club(club_id)
        existing = entries.filter(source_namespace=namespace, source_key=key, kind=kind).first()
        if existing is not None:
            if existing.payload_fingerprint != fingerprint:
                _fail("Ключ уже принят с другими условиями.", "idempotency_conflict")
            return existing if apply else {"replay": True, "entry_id": existing.id, "payload_fingerprint": fingerprint}
        # Reversal and corrections drain existing state with new writes off.
        if apply and kind in {"opening", "payout"} and not settings.TRAINER_SETTLEMENTS_ENABLED:
            _fail("Новые расчётные записи временно отключены.", "settlement_writes_disabled")
        if effective_on > club_localdate(club):
            _fail("Нельзя записать будущую выплату или сверку.", "settlement_future_date")
        opening = entries.filter(trainer_id=trainer_id, kind="opening").first()
        reversal = None
        summary = _summary(club=club, trainer_id=trainer_id, day=effective_on)
        if expected_fingerprint and expected_fingerprint != summary["fingerprint"]:
            _fail("Данные изменились. Проверьте новый остаток; ваш ввод сохранён.", "settlement_stale_preview")
        if kind == "opening":
            if opening is not None:
                _fail("Начальная сверка уже записана. Используйте исправление.", "settlement_opening_exists")
            if signed is None or cash is not None or reversal_of_id is not None or payment_method:
                _fail("Для сверки требуется только долг или аванс.", "settlement_invalid_fields")
            _assert_salary_ready(club=club, trainer_id=trainer_id, through=effective_on - timedelta(days=1))
        else:
            if opening is None:
                _fail("Сначала подтвердите начальный долг, аванс или нулевой остаток.", "settlement_opening_required")
            if kind == "opening_correction":
                if signed is None or cash is not None or reversal_of_id is not None or payment_method:
                    _fail("Для исправления требуется изменение остатка.", "settlement_invalid_fields")
                if effective_on < opening.effective_on:
                    _fail("Исправление не может быть раньше начальной сверки.", "settlement_invalid_date")
            elif kind == "payout":
                if (
                    signed is not None
                    or cash is None
                    or cash <= 0
                    or reversal_of_id is not None
                    or payment_method not in {"cash", "transfer"}
                ):
                    _fail("Укажите положительную сумму и способ выплаты.", "settlement_invalid_fields")
                if summary["unresolved_ids"]:
                    _fail(
                        "Сначала разрешите исторические изменения начального остатка.",
                        "settlement_reconciliation_required",
                    )
                _assert_salary_ready(club=club, trainer_id=trainer_id, through=effective_on)
                if (summary["balance"] is None or cash > summary["balance"]) and not confirm_advance:
                    _fail(
                        "Выплата создаёт аванс или требует исторической сверки. Подтвердите её явно.",
                        "settlement_advance_confirmation",
                    )
                signed = -cash
            else:
                if signed is not None or cash is not None or not reversal_of_id or payment_method:
                    _fail("Отмена должна ссылаться на точную выплату.", "settlement_invalid_fields")
                reversal = (
                    entries.select_for_update().filter(id=reversal_of_id, trainer_id=trainer_id, kind="payout").first()
                )
                if reversal is None:
                    _fail("Выплата недоступна.", "target_not_available")
                if entries.filter(reversal_of=reversal).exists():
                    _fail("Выплата уже отменена.", "settlement_already_reversed")
                if effective_on < reversal.effective_on:
                    _fail("Отмена не может быть раньше выплаты.", "settlement_invalid_date")
                signed, cash = reversal.amount, reversal.amount
        if not apply:
            return {
                "replay": False,
                "opening_id": opening.id if opening else None,
                "payload_fingerprint": fingerprint,
                "fingerprint": summary["fingerprint"],
                "balance_before": summary["balance"],
                "balance_delta": signed,
                "scope": summary["scope"],
            }
        entry = TrainerSettlementEntry.objects.create(
            club=club,
            trainer_id=trainer_id,
            kind=kind,
            effective_on=effective_on,
            balance_delta=signed,
            amount=cash,
            payment_method=payment_method,
            opening=opening,
            reversal_of=reversal,
            actor_id=actor_user_id,
            reason=reason,
            channel=channel,
            source_namespace=namespace,
            source_key=key,
            payload_fingerprint=fingerprint,
            command_payload=payload,
        )
        if opening is not None and effective_on < opening.effective_on:
            record_historical_settlement_change(
                club_id=club_id,
                trainer_id=trainer_id,
                effective_on=effective_on,
                event_key=f"settlement:{entry.id}",
                suggested_delta=signed,
                evidence={"entry_id": entry.id, "kind": kind},
            )
        return entry


def resolve_trainer_settlement(
    *,
    club_id: int,
    actor_user_id: int,
    case_id: int,
    action: str,
    reason: str,
    source_namespace: str,
    source_key: str,
    balance_delta=None,
    effective_on=None,
):
    namespace, key, reason = _identity(source_namespace=source_namespace, source_key=source_key, reason=reason)
    signed = _money(balance_delta) if balance_delta is not None else None
    payload = {
        "case_id": case_id,
        "action": action,
        "reason": reason,
        "balance_delta": str(signed) if signed is not None else None,
        "effective_on": str(effective_on),
    }
    fingerprint = _hash(payload)
    with transaction.atomic():
        Club.objects.select_for_update(no_key=True).get(id=club_id)
        _authorize(club_id=club_id, actor_user_id=actor_user_id)
        case = TrainerSettlementReconciliation.objects.for_club(club_id).filter(id=case_id).first()
        if case is None:
            _fail("Сверка недоступна.", "target_not_available")
        _lock_trainers(club_id=club_id, trainer_ids=[case.trainer_id])
        existing = (
            TrainerSettlementResolution.objects.for_club(club_id)
            .filter(
                source_namespace=namespace,
                source_key=key,
            )
            .first()
        )
        if existing:
            if existing.payload_fingerprint != fingerprint:
                _fail("Ключ уже принят с другим решением.", "idempotency_conflict")
            return existing
        if TrainerSettlementResolution.objects.for_club(club_id).filter(case=case).exists():
            _fail("Сверка уже разрешена.", "settlement_already_resolved")
        correction = None
        if action == "adjust_opening":
            correction = record_trainer_settlement(
                club_id=club_id,
                actor_user_id=actor_user_id,
                trainer_id=case.trainer_id,
                kind="opening_correction",
                effective_on=effective_on,
                balance_delta=signed,
                reason=reason,
                source_namespace=namespace,
                source_key=key,
            )
        elif action != "already_included" or signed is not None or effective_on is not None:
            _fail("Подтвердите учёт факта или укажите отдельное исправление.", "settlement_invalid_resolution")
        return TrainerSettlementResolution.objects.create(
            club_id=club_id,
            case=case,
            action=action,
            correction=correction,
            actor_id=actor_user_id,
            reason=reason,
            source_namespace=namespace,
            source_key=key,
            payload_fingerprint=fingerprint,
            command_payload=payload,
        )


def _assert_salary_ready(*, club, trainer_id, through):
    from datetime import timedelta

    from django.db.models import Q

    from apps.attendance.models import CheckinCascadeEvent
    from apps.billing.models import Payment, SubscriptionComponent
    from apps.clubs.timezones import club_local_day_start

    pending_visit = (
        CheckinCascadeEvent.objects.for_club(club)
        .filter(
            effect="salary",
            expected=True,
            checkin__date__lte=through,
            checkin__cancelled_at__isnull=True,
            checkin__deleted_at__isnull=True,
            checkin__trainerearning__isnull=True,
        )
        .filter(Q(payload__trainer_id_snapshot=trainer_id) | Q(checkin__trainer_id=trainer_id))
        .exclude(
            checkin__trainer_earning_adjustments__kind="late_drop_in_credit",
            checkin__trainer_earning_adjustments__direction="credit",
        )
        .exists()
    )
    upper = club_local_day_start(club, through + timedelta(days=1))
    eligible_date = Q(origin="opening", opening_effective_on__lte=through) | Q(
        origin="ordinary",
        verified_at__lt=upper,
    )
    pending_sale = (
        Payment.objects.for_club(club)
        .filter(
            eligible_date,
            status="confirmed",
            sale_earning_snapshot_recorded=True,
            sale_trainer_id_snapshot=trainer_id,
            sale_rate_percent_snapshot__isnull=False,
            sale_amount_basis_snapshot__isnull=False,
            earnings__isnull=True,
        )
        .exists()
    )
    pending_component = (
        SubscriptionComponent.objects.for_club(club)
        .filter(
            Q(subscription__payment__origin="opening", subscription__payment__opening_effective_on__lte=through)
            | Q(subscription__payment__origin="ordinary", subscription__payment__verified_at__lt=upper),
            trainer_payout_policy_snapshot="on_payment",
            subscription__payment__status="confirmed",
            sale_trainer_id_snapshot=trainer_id,
            sale_rate_percent_snapshot__isnull=False,
            trainer_earnings__isnull=True,
        )
        .exists()
    )
    if pending_visit or pending_sale or pending_component:
        _fail("Сначала завершите ожидающие начисления тренера.", "settlement_pending_salary")


def record_trainer_settlement(
    *,
    club_id: int,
    actor_user_id: int,
    trainer_id: int,
    kind: str,
    effective_on: date,
    reason: str,
    source_namespace: str,
    source_key: str,
    balance_delta=None,
    amount=None,
    reversal_of_id=None,
    payment_method="",
    confirm_advance=False,
    expected_fingerprint=None,
    channel="admin",
):
    return _settlement_command(
        club_id=club_id,
        actor_user_id=actor_user_id,
        trainer_id=trainer_id,
        kind=kind,
        effective_on=effective_on,
        reason=reason,
        source_namespace=source_namespace,
        source_key=source_key,
        balance_delta=balance_delta,
        amount=amount,
        reversal_of_id=reversal_of_id,
        payment_method=payment_method,
        confirm_advance=confirm_advance,
        expected_fingerprint=expected_fingerprint,
        channel=channel,
        apply=True,
    )


def preview_trainer_settlement(
    *,
    club_id: int,
    actor_user_id: int,
    trainer_id: int,
    kind: str,
    effective_on: date,
    reason: str,
    source_namespace: str,
    source_key: str,
    balance_delta=None,
    amount=None,
    reversal_of_id=None,
    payment_method="",
    confirm_advance=False,
    channel="admin",
):
    return _settlement_command(
        club_id=club_id,
        actor_user_id=actor_user_id,
        trainer_id=trainer_id,
        kind=kind,
        effective_on=effective_on,
        reason=reason,
        source_namespace=source_namespace,
        source_key=source_key,
        balance_delta=balance_delta,
        amount=amount,
        reversal_of_id=reversal_of_id,
        payment_method=payment_method,
        confirm_advance=confirm_advance,
        channel=channel,
        apply=False,
    )
