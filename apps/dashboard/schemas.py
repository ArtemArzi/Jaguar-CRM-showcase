from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Literal

from ninja import Schema


class DashboardMetricsOut(Schema):
    active_subscriptions: int
    expiring_subscriptions: int
    debtors: int
    checkins: int
    revenue: Decimal
    new_students: int


class AttentionAlertOut(Schema):
    type: Literal[
        "expiring_subscriptions",
        "unconfirmed_payments",
        "at_risk_students",
        "overdue_retention_tasks",
    ]
    count: int


class DashboardFilters(Schema):
    date_from: date
    date_to: date


class PnlReportOut(Schema):
    gross_income: Decimal
    refunded_income: Decimal
    income: Decimal
    salary_expenses: Decimal
    manual_expenses: Decimal
    margin: Decimal


class BusinessMetricsOut(Schema):
    arpm: Decimal | None
    churn_rate: Decimal | None
    retention_rate: Decimal | None
    ltv: Decimal | None
