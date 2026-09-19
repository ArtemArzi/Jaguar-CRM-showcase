from datetime import date
from decimal import Decimal

from ninja import Schema


class TrainerRateIn(Schema):
    location_id: int
    training_type_id: int
    percent: Decimal


class TrainerLocationIn(Schema):
    location_id: int
    # Legacy per-kind defaults remain as optional keys so old API clients
    # continue to work; the service expands them into per-type rates.
    rate_group: Decimal = Decimal("20.00")
    rate_personal: Decimal = Decimal("50.00")
    rate_mini_group: Decimal = Decimal("40.00")
    rates: list[TrainerRateIn] = []


class TrainerLocationOut(Schema):
    id: int
    location_id: int
    location_name: str = ""

    @staticmethod
    def resolve_location_name(obj):
        return obj.location.name


class TrainerIn(Schema):
    first_name: str
    last_name: str
    phone: str = ""
    locations: list[TrainerLocationIn] = []


class TrainerOut(Schema):
    id: int
    first_name: str
    last_name: str
    phone: str
    is_active: bool
    locations: list[TrainerLocationOut] = []
    student_count: int = 0

    @staticmethod
    def resolve_locations(obj):
        if hasattr(obj, "trainer_locations"):
            return obj.trainer_locations.all()
        return []

    @staticmethod
    def resolve_student_count(obj) -> int:
        if hasattr(obj, "_student_count"):
            return obj._student_count
        return obj.assigned_leads.count()


class TrainerUpdate(Schema):
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    is_active: bool | None = None


# ──────────────────────────────────────────────
# Salary / Earnings schemas
# ──────────────────────────────────────────────


class TrainerEarningOut(Schema):
    id: int
    earning_type: str
    amount: Decimal
    rate_percent: Decimal
    subscription_price: Decimal | None = None
    checkin_date: str = ""
    schedule_name: str = ""
    package_owner_trainer_id: int | None = None
    package_owner_trainer_name: str = ""
    package_transfer_amount_basis: Decimal | None = None
    package_transfer_payable_delta: Decimal | None = None
    package_transfer_affects_payroll: bool | None = None
    package_transfer_reason: str = ""

    @staticmethod
    def resolve_checkin_date(obj) -> str:
        if hasattr(obj, "checkin") and obj.checkin:
            return str(obj.checkin.date)
        return ""

    @staticmethod
    def resolve_schedule_name(obj) -> str:
        if hasattr(obj, "checkin") and obj.checkin and hasattr(obj.checkin, "schedule") and obj.checkin.schedule:
            return obj.checkin.schedule.group_name
        return ""

    @staticmethod
    def _package_transfer(obj):
        if not hasattr(obj, "checkin") or not obj.checkin:
            return None
        adjustments = getattr(obj.checkin, "package_transfer_adjustments", None)
        if adjustments is not None:
            return adjustments[0] if adjustments else None
        from apps.trainers.models import TrainerEarningAdjustment

        return (
            obj.checkin.trainer_earning_adjustments.filter(
                club_id=obj.club_id,
                trainer_id=obj.trainer_id,
                kind=TrainerEarningAdjustment.Kind.PACKAGE_TRANSFER,
                direction=TrainerEarningAdjustment.Direction.INFO,
            )
            .select_related("counterparty_trainer")
            .first()
        )

    @staticmethod
    def resolve_package_owner_trainer_id(obj) -> int | None:
        transfer = TrainerEarningOut._package_transfer(obj)
        return transfer.counterparty_trainer_id if transfer else None

    @staticmethod
    def resolve_package_owner_trainer_name(obj) -> str:
        transfer = TrainerEarningOut._package_transfer(obj)
        return str(transfer.counterparty_trainer) if transfer and transfer.counterparty_trainer else ""

    @staticmethod
    def resolve_package_transfer_amount_basis(obj) -> Decimal | None:
        transfer = TrainerEarningOut._package_transfer(obj)
        return transfer.amount_basis_snapshot if transfer else None

    @staticmethod
    def resolve_package_transfer_payable_delta(obj) -> Decimal | None:
        transfer = TrainerEarningOut._package_transfer(obj)
        return transfer.payable_amount_delta if transfer else None

    @staticmethod
    def resolve_package_transfer_affects_payroll(obj) -> bool | None:
        transfer = TrainerEarningOut._package_transfer(obj)
        return transfer.affects_payroll if transfer else None

    @staticmethod
    def resolve_package_transfer_reason(obj) -> str:
        transfer = TrainerEarningOut._package_transfer(obj)
        return transfer.reason if transfer else ""


class TrainerSalaryLedgerRowOut(Schema):
    id: int
    row_type: str
    earning_type: str
    amount: Decimal
    rate_percent: Decimal
    subscription_price: Decimal | None = None
    checkin_date: str = ""
    schedule_name: str = ""
    package_owner_trainer_id: int | None = None
    package_owner_trainer_name: str = ""
    package_transfer_amount_basis: Decimal | None = None
    package_transfer_payable_delta: Decimal | None = None
    package_transfer_affects_payroll: bool | None = None
    package_transfer_reason: str = ""
    adjustment_direction: str = ""
    adjustment_reason: str = ""
    adjustment_effective_date: str = ""
    adjustment_affects_payroll: bool | None = None
    source_checkin_id: int | None = None
    source_checkin_date: str = ""
    source_schedule_name: str = ""
    source_payment_id: int | None = None


class EarningSummaryOut(Schema):
    total_amount: Decimal
    total_sessions: int
    by_type: dict


class SalarySummaryOut(Schema):
    trainer_id: int
    trainer_name: str = ""
    total: Decimal
    sessions: int

    @staticmethod
    def resolve_trainer_name(obj) -> str:
        first = obj.get("trainer__first_name", "")
        last = obj.get("trainer__last_name", "")
        return f"{first} {last}".strip()


class TrainerSettlementSummaryOut(Schema):
    earned: Decimal
    paid: Decimal
    reversed: Decimal
    net_paid: Decimal
    balance: Decimal | None
    status: str
    date_from: date
    date_to: date
    opening_on: date | None
    unresolved_count: int
