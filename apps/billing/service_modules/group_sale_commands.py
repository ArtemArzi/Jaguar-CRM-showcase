from __future__ import annotations

from datetime import date

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import validate_email
from django.db import transaction

from apps.billing.models import BankPaymentOrder, Payment
from apps.billing.service_modules.bank_orders import create_bank_payment_order
from apps.billing.service_modules.group_payments import (
    _replay_v2_group_sale_command,
    _v2_group_sale_command_fingerprint,
    assert_v2_group_sale_offer_digest,
    resolve_v2_group_sale_offer,
)
from apps.billing.service_modules.payment_creation import create_payment
from apps.billing.service_modules.payment_readiness import get_online_payment_capability
from apps.clubs.capabilities import (
    get_commercial_journey_capability,
    get_v2_group_manual_admission_command_availability,
    get_v2_group_provider_command_availability,
    get_v2_provider_command_availability,
)
from apps.clubs.models import ClubSettings
from apps.common.exceptions import BusinessLogicError


def create_v2_group_sale_manual(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int,
    payment_method: str,
    target_training_group_id: int,
    target_schedule_id: int,
    target_start_date: date,
    expected_offer_digest: str,
    command_idempotency_key: str,
    recorded_by_id: int,
    enforce_trainer_group_contract: bool,
) -> Payment:
    """Create the strict cash/transfer v2 family from a signed group offer."""

    command_key = command_idempotency_key.strip()
    if not command_key:
        raise BusinessLogicError(
            "Контекстная команда оплаты требует стабильный ключ",
            code="idempotency_key_required",
        )
    fingerprint = _v2_group_sale_command_fingerprint(
        student_id=student_id,
        tariff_id=tariff_id,
        payment_method=payment_method,
        target_training_group_id=target_training_group_id,
        target_schedule_id=target_schedule_id,
        target_start_date=target_start_date,
        expected_offer_digest=expected_offer_digest,
    )
    with transaction.atomic():
        replay = _replay_v2_group_sale_command(
            club_id=club_id,
            command_idempotency_key=command_key,
            command_fingerprint=fingerprint,
        )
        if replay is not None:
            return replay

    def validate_locked_v2_offer():
        ClubSettings.objects.select_for_update(of=("self",)).filter(
            club_id=club_id,
        ).first()
        offer = resolve_v2_group_sale_offer(
            club_id=club_id,
            student_id=student_id,
            tariff_id=tariff_id,
            target_training_group_id=target_training_group_id,
            target_schedule_id=target_schedule_id,
            target_start_date=target_start_date,
            lock=True,
        )
        assert_v2_group_sale_offer_digest(
            offer=offer,
            expected_offer_digest=expected_offer_digest,
        )
        availability = get_v2_group_manual_admission_command_availability(
            capability=get_commercial_journey_capability(club=club_id),
            training_group_rollout_mode=offer.rollout_state.mode,
            training_group_new_writes_enabled=bool(
                settings.TRAINING_GROUP_NEW_WRITES_ENABLED
            ),
        )
        if not availability.allows_new_command:
            raise BusinessLogicError(
                "Commercial journey command is unavailable for this client or tenant.",
                code=availability.code,
            )
        return offer.rollout_state

    return create_payment(
        club_id=club_id,
        student_id=student_id,
        tariff_id=tariff_id,
        payment_method=payment_method,
        discount_ids=[],
        debt_ids=[],
        recorded_by_id=recorded_by_id,
        target_training_group_id=target_training_group_id,
        target_schedule_id=target_schedule_id,
        target_start_date=target_start_date,
        enforce_trainer_group_contract=enforce_trainer_group_contract,
        create_manual_operational_admission=True,
        command_idempotency_key=command_key,
        command_fingerprint=fingerprint,
        _locked_pre_create_validator=validate_locked_v2_offer,
    )


def create_v2_group_sale_bank_order(
    *,
    club_id: int,
    student_id: int,
    tariff_id: int,
    target_training_group_id: int,
    target_schedule_id: int,
    target_start_date: date,
    expected_offer_digest: str,
    command_idempotency_key: str,
    created_by_id: int,
    source: str,
    buyer_email: str | None,
    enforce_trainer_group_contract: bool,
):
    """Create the strict provider-pending v2 family without lead admission."""

    command_key = command_idempotency_key.strip()
    if not command_key:
        raise BusinessLogicError(
            "Контекстная команда оплаты требует стабильный ключ",
            code="idempotency_key_required",
        )
    fingerprint = _v2_group_sale_command_fingerprint(
        student_id=student_id,
        tariff_id=tariff_id,
        payment_method=Payment.Method.ONLINE,
        target_training_group_id=target_training_group_id,
        target_schedule_id=target_schedule_id,
        target_start_date=target_start_date,
        expected_offer_digest=expected_offer_digest,
        buyer_email=buyer_email,
    )
    with transaction.atomic():
        replay = _replay_v2_group_sale_command(
            club_id=club_id,
            command_idempotency_key=command_key,
            command_fingerprint=fingerprint,
        )
        if replay is not None:
            order = (
                BankPaymentOrder.objects.for_club(club_id)
                .filter(payment_id=replay.id)
                .order_by("-id")
                .first()
            )
            if order is None:
                raise BusinessLogicError(
                    "Команда оплаты требует ручной проверки",
                    code="command_replay_order_missing",
                )
            order._command_replayed = True
            return order

    normalized_email = (buyer_email or "").strip()
    if settings.TOCHKA_RECEIPT_MODE == "tochka_receipt":
        if not normalized_email:
            raise BusinessLogicError(
                "Для фискального чека нужен email покупателя",
                code="receipt_buyer_email_required",
            )
        try:
            validate_email(normalized_email)
        except ValidationError as exc:
            raise BusinessLogicError(
                "Укажите корректный email для фискального чека",
                code="receipt_buyer_email_invalid",
            ) from exc

    def validate_locked_v2_offer() -> None:
        ClubSettings.objects.select_for_update(of=("self",)).filter(
            club_id=club_id,
        ).first()
        offer = resolve_v2_group_sale_offer(
            club_id=club_id,
            student_id=student_id,
            tariff_id=tariff_id,
            target_training_group_id=target_training_group_id,
            target_schedule_id=target_schedule_id,
            target_start_date=target_start_date,
            lock=True,
        )
        assert_v2_group_sale_offer_digest(
            offer=offer,
            expected_offer_digest=expected_offer_digest,
        )
        if offer.payload["buyer_email_required"]:
            if not normalized_email:
                raise BusinessLogicError(
                    "Для фискального чека нужен email покупателя",
                    code="receipt_buyer_email_required",
                )
            try:
                validate_email(normalized_email)
            except ValidationError as exc:
                raise BusinessLogicError(
                    "Укажите корректный email для фискального чека",
                    code="receipt_buyer_email_invalid",
                ) from exc
        payment_capability = get_online_payment_capability()
        availability = get_v2_group_provider_command_availability(
            capability=get_commercial_journey_capability(club=club_id),
            provider_creation_enabled=payment_capability.enabled,
            training_group_rollout_mode=offer.rollout_state.mode,
            training_group_new_writes_enabled=bool(
                settings.TRAINING_GROUP_NEW_WRITES_ENABLED
            ),
        )
        if not availability.allows_new_command:
            raise BusinessLogicError(
                "Commercial journey command is unavailable for this client or tenant.",
                code=availability.code,
            )

    def validate_locked_v2_protocol() -> None:
        """Arbitrate a distinct key against protocol before family reuse."""

        ClubSettings.objects.select_for_update(of=("self",)).filter(
            club_id=club_id,
        ).first()
        payment_capability = get_online_payment_capability()
        availability = get_v2_provider_command_availability(
            capability=get_commercial_journey_capability(club=club_id),
            provider_creation_enabled=payment_capability.enabled,
        )
        if not availability.allows_new_command:
            raise BusinessLogicError(
                "Commercial journey command is unavailable for this client or tenant.",
                code=availability.code,
            )

    return create_bank_payment_order(
        club_id=club_id,
        student_id=student_id,
        tariff_id=tariff_id,
        source=source,
        created_by_id=created_by_id,
        discount_ids=[],
        debt_ids=[],
        target_training_group_id=target_training_group_id,
        target_schedule_id=target_schedule_id,
        target_start_date=target_start_date,
        buyer_email=buyer_email,
        enforce_trainer_group_contract=enforce_trainer_group_contract,
        command_idempotency_key=command_key,
        command_fingerprint=fingerprint,
        reject_reusable_order_for_distinct_key=True,
        _locked_pre_reuse_validator=validate_locked_v2_protocol,
        _locked_pre_create_validator=validate_locked_v2_offer,
    )
