"""Explicit workbook column mapping and normalization, without ORM mutations."""

from datetime import datetime

from django.utils import timezone

from apps.billing.service_modules.opening_terms import OpeningEntitlementTerms
from apps.students.imports.parsing import invalid, parse_date, parse_money, parse_phone

COLUMNS = {
    "student_source_key": "student_key",
    "entitlement_source_key": "source_subscription_key",
    "payment_source_key": "source_payment_key",
    "first_name": "Имя",
    "last_name": "Фамилия",
    "is_child": "Ребёнок",
    "phone": "Телефон",
    "guardian_phone": "Телефон родителя",
    "date_of_birth": "Дата рождения",
    "student_id": "ID ученика",
    "tariff_id": "ID тарифа",
    "started_on": "Начало",
    "expires_on": "Действует по",
    "effective_on": "Дата оплаты",
    "covered_through": "Сверено по",
    "operational_cutover": "Начало работы в CRM",
    "original_total": "Всего занятий",
    "original_used": "Использовано",
    "original_left": "Осталось",
    "paid_amount": "Оплачено",
    "payout_policy": "Правило начислений",
    "payment_method": "Способ оплаты",
    "external_gap_confirmed": "После сверки изменений не было",
    "past_training_confirmed": "Ранее занимался",
    "confirm_distinct_child": "Подтверждён отдельный ребёнок",
    "confirm_student_transition": "Подтверждено возобновление или переход",
    "assigned_trainer_id": "ID ответственного тренера",
    "change_assigned_trainer": "Изменить ответственного",
    "package_owner_trainer_id": "ID владельца пакета",
    "sale_trainer_id": "ID получателя комиссии",
    "sale_rate_percent": "Исходный процент комиссии",
    "training_group_id": "ID группы",
    "schedule_id": "ID занятия группы",
    "cutover_schedule_id": "ID первого персонального занятия",
    "distinct_payment_reference": "Основание отдельной оплаты",
    "source_note": "Комментарий",
}
EXTRA_COLUMNS = (
    "Исходная цена",
    "Валюта",
    "Исторический долг",
    "Заморожен",
    "Тип пакета",
    "Тариф",
    "Группа",
    "Ответственный тренер",
    "Владелец пакета",
    "Получатель комиссии",
)
DATE_FIELDS = {"started_on", "expires_on", "effective_on", "date_of_birth"}
TIME_FIELDS = {"covered_through", "operational_cutover"}
INT_FIELDS = {
    "student_id",
    "tariff_id",
    "original_total",
    "original_used",
    "original_left",
    "assigned_trainer_id",
    "package_owner_trainer_id",
    "sale_trainer_id",
    "training_group_id",
    "schedule_id",
    "cutover_schedule_id",
}
BOOL_FIELDS = {
    "is_child",
    "external_gap_confirmed",
    "past_training_confirmed",
    "confirm_distinct_child",
    "confirm_student_transition",
    "change_assigned_trainer",
}
OPTIONAL_FIELDS = {
    "student_id",
    "date_of_birth",
    "assigned_trainer_id",
    "package_owner_trainer_id",
    "sale_trainer_id",
    "sale_rate_percent",
    "training_group_id",
    "schedule_id",
    "cutover_schedule_id",
}
OPTIONAL_BOOLEANS = {"confirm_distinct_child", "confirm_student_transition", "change_assigned_trainer"}


def boolean(value):
    if value is True or value == "Да":
        return True
    if value is False or value == "Нет":
        return False
    invalid("Укажите явно «Да» или «Нет».", "import_invalid_value")


def integer(value):
    if isinstance(value, bool):
        invalid("Укажите целое число.", "import_invalid_value")
    try:
        result = int(value)
        if str(result) != str(value).strip() and result != value:
            raise ValueError
        if not 0 <= result <= 2147483647:
            raise ValueError
        return result
    except (TypeError, ValueError, OverflowError):
        invalid("Укажите целое неотрицательное число.", "import_invalid_value")


def instant(value, *, zone):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(value)
        if timezone.is_naive(result):
            # Reject ambiguous/nonexistent local times instead of choosing a fold.
            first, second = result.replace(tzinfo=zone, fold=0), result.replace(tzinfo=zone, fold=1)
            if first.utcoffset() != second.utcoffset():
                raise ValueError
            result = first
        return result
    except (TypeError, ValueError):
        invalid("Укажите точный момент с часовым поясом клуба.", "import_invalid_value")


def terms_from_row(*, values: dict, namespace: str, zone) -> OpeningEntitlementTerms:
    if values.get("Валюта") != "RUB" or values.get("Тип пакета") not in {"Групповой", "Персональный"}:
        invalid("Поддержаны конечные групповые и персональные пакеты в RUB.", "opening_needs_review")
    if parse_money(values.get("Исторический долг")) != 0 or boolean(values.get("Заморожен")):
        invalid("Долг или заморозка требуют отдельной сверки.", "opening_needs_review")
    paid = parse_money(values.get("Оплачено"))
    if parse_money(values.get("Исходная цена")) != paid:
        invalid("Перенос частичной оплаты требует отдельной сверки.", "opening_needs_review")
    normalized = {"source_namespace": namespace}
    for field, column in COLUMNS.items():
        value = values.get(column)
        if value in (None, "") and field in OPTIONAL_FIELDS:
            normalized[field] = None
        elif value in (None, "") and field in OPTIONAL_BOOLEANS:
            normalized[field] = False
        elif field in DATE_FIELDS:
            normalized[field] = parse_date(value)
        elif field in TIME_FIELDS:
            normalized[field] = instant(value, zone=zone)
        elif field in INT_FIELDS:
            normalized[field] = integer(value)
        elif field in BOOL_FIELDS:
            normalized[field] = boolean(value)
        elif field in {"paid_amount", "sale_rate_percent"}:
            normalized[field] = parse_money(value)
        elif field in {"phone", "guardian_phone"}:
            normalized[field] = parse_phone(value)
        elif field == "payout_policy":
            normalized[field] = {"С оплаты": "on_payment", "За занятие": "on_checkin", "Без начисления": "none"}.get(
                value, value
            )
        elif field == "payment_method":
            normalized[field] = {"Наличные": "cash", "Перевод": "transfer", "Неизвестен": "unknown"}.get(value, value)
        else:
            normalized[field] = str(value).strip() if value is not None else ""
    # Construct only from the declared mapping, never raw workbook dictionaries.
    return OpeningEntitlementTerms(**normalized).normalized()


def terms_from_payload(payload: dict) -> OpeningEntitlementTerms:
    values = {column: payload.get(field) for field, column in COLUMNS.items()}
    normalized = {"source_namespace": payload["source_namespace"]}
    for field, column in COLUMNS.items():
        value = values[column]
        if value is not None and field in DATE_FIELDS:
            value = parse_date(value)
        elif value is not None and field in TIME_FIELDS:
            value = datetime.fromisoformat(value)
        elif value is not None and field in {"paid_amount", "sale_rate_percent"}:
            value = parse_money(value)
        normalized[field] = value
    return OpeningEntitlementTerms(**normalized).normalized()
