"""Typed reviewed opening terms, shared by future workbook and card adapters."""

import json
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256

from django.utils import timezone

from apps.billing.models import Payment, Tariff
from apps.common.exceptions import BusinessLogicError
from apps.students.identity_services import normalize_person_identity


@dataclass(frozen=True)
class OpeningEntitlementTerms:
    source_namespace: str
    student_source_key: str
    entitlement_source_key: str
    payment_source_key: str
    first_name: str
    last_name: str
    is_child: bool
    phone: str
    guardian_phone: str
    date_of_birth: date | None
    tariff_id: int
    started_on: date
    expires_on: date
    effective_on: date
    covered_through: datetime
    operational_cutover: datetime
    original_total: int
    original_used: int
    original_left: int
    paid_amount: Decimal
    payout_policy: str
    payment_method: str
    external_gap_confirmed: bool
    past_training_confirmed: bool
    student_id: int | None = None
    confirm_distinct_child: bool = False
    confirm_student_transition: bool = False
    assigned_trainer_id: int | None = None
    change_assigned_trainer: bool = False
    package_owner_trainer_id: int | None = None
    sale_trainer_id: int | None = None
    sale_rate_percent: Decimal | None = None
    training_group_id: int | None = None
    schedule_id: int | None = None
    cutover_schedule_id: int | None = None
    distinct_payment_reference: str = ""
    source_note: str = ""

    def normalized(self):
        """Reject unknown source facts before any domain mutation."""
        for value in (
            self.source_namespace, self.student_source_key,
            self.entitlement_source_key, self.payment_source_key,
        ):
            if not isinstance(value, str) or not value.strip() or len(value.strip()) > 120:
                _needs_review("Укажите устойчивые ключи источника, ученика, пакета и оплаты.")
        for value in (self.started_on, self.expires_on, self.effective_on):
            if not isinstance(value, date) or isinstance(value, datetime):
                _needs_review("Укажите точные исходные даты.")
        for value in (self.covered_through, self.operational_cutover):
            if not isinstance(value, datetime) or timezone.is_naive(value):
                _needs_review("Граница сверки и начало работы требуют точного времени с часовым поясом.")
        if self.expires_on < self.started_on or self.covered_through >= self.operational_cutover:
            _needs_review("Проверьте порядок исходных дат и границу сверки.")
        if not self.external_gap_confirmed:
            raise BusinessLogicError(
                "Подтвердите отсутствие внешних изменений после сверки или обновите остаток.",
                code="opening_cutover_needs_review",
            )
        if not self.past_training_confirmed:
            _needs_review("Подтвердите, что ученик ранее занимался в клубе.")
        counts = (self.original_total, self.original_used, self.original_left)
        if any(type(value) is not int or value < 0 for value in counts):
            _needs_review("Исходные лимит, использовано и остаток должны быть целыми неотрицательными числами.")
        if self.original_total <= 0 or self.original_total != self.original_used + self.original_left:
            _needs_review("Исходный лимит должен совпадать с суммой использованного и остатка.")
        if self.original_total > 2147483647:
            _needs_review("Исходный лимит превышает допустимый размер.")
        if (
            not isinstance(self.paid_amount, Decimal)
            or not self.paid_amount.is_finite()
            or not Decimal("0") < self.paid_amount <= Decimal("99999999.99")
            or self.paid_amount != self.paid_amount.quantize(Decimal("0.01"))
        ):
            _needs_review("Укажите полностью оплаченную исходную сумму с точностью до копеек.")
        if self.payment_method not in {Payment.Method.CASH, Payment.Method.TRANSFER, Payment.Method.UNKNOWN}:
            _needs_review("Исторический способ оплаты требует сверки.")
        if self.payout_policy not in Tariff.PayoutPolicy.values:
            _needs_review("Подтвердите исходные условия начислений.")
        if self.payout_policy == Tariff.PayoutPolicy.ON_PAYMENT:
            rate = self.sale_rate_percent
            if (
                self.sale_trainer_id is None or not isinstance(rate, Decimal) or not rate.is_finite()
                or not Decimal("0") <= rate <= Decimal("100")
                or rate != rate.quantize(Decimal("0.01"))
            ):
                _needs_review("Подтвердите получателя и исходный процент комиссии.")
        elif self.sale_rate_percent is not None or self.sale_trainer_id is not None:
            _needs_review("Комиссия с оплаты несовместима с выбранным порядком начислений.")
        first_name, last_name = self.first_name.strip(), self.last_name.strip()
        if not first_name or len(first_name) > 100 or len(last_name) > 100:
            _needs_review("Проверьте имя ученика.")
        identity = normalize_person_identity(
            first_name=first_name, last_name=last_name, is_child=self.is_child,
            phone=self.phone, guardian_phone=self.guardian_phone, date_of_birth=self.date_of_birth,
        )
        return replace(
            self, source_namespace=self.source_namespace.strip(),
            student_source_key=self.student_source_key.strip(),
            entitlement_source_key=self.entitlement_source_key.strip(),
            payment_source_key=self.payment_source_key.strip(),
            first_name=identity.first_name, last_name=identity.last_name,
            phone=identity.phone, guardian_phone=identity.guardian_phone,
            paid_amount=self.paid_amount.quantize(Decimal("0.01")),
            sale_rate_percent=(
                self.sale_rate_percent.quantize(Decimal("0.01")) if self.sale_rate_percent is not None else None
            ),
            distinct_payment_reference=self.distinct_payment_reference.strip(), source_note=self.source_note.strip(),
        )

    def as_payload(self):
        return json.loads(json.dumps(asdict(self), default=_json_value, sort_keys=True))

    def fingerprint(self):
        return sha256(json.dumps(self.as_payload(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _json_value(value):
    if isinstance(value, datetime):
        from datetime import UTC

        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError("Unsupported opening value")


def _needs_review(message):
    raise BusinessLogicError(message, code="opening_needs_review")
