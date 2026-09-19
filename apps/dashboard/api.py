from ninja import Query, Router

from apps.common.permissions import role_required
from apps.dashboard.schemas import (
    AttentionAlertOut,
    BusinessMetricsOut,
    DashboardFilters,
    DashboardMetricsOut,
    PnlReportOut,
)
from apps.dashboard.selectors import get_attention_alerts, get_dashboard_metrics
from apps.dashboard.services import get_business_metrics, get_pnl_report

router = Router(tags=["dashboard"])


@router.get("/metrics/", response=DashboardMetricsOut)
@role_required("owner", "admin")
def dashboard_metrics(request, filters: Query[DashboardFilters]):
    return get_dashboard_metrics(
        club=request.club,
        date_from=filters.date_from,
        date_to=filters.date_to,
    )


@router.get("/alerts/", response=list[AttentionAlertOut])
@role_required("owner", "admin")
def attention_alerts(request):
    return get_attention_alerts(club=request.club)


@router.get("/pnl/", response=PnlReportOut)
@role_required("owner", "admin")
def pnl_report(request, filters: Query[DashboardFilters]):
    return get_pnl_report(
        club=request.club,
        date_from=filters.date_from,
        date_to=filters.date_to,
    )


@router.get("/business-metrics/", response=BusinessMetricsOut)
@role_required("owner", "admin")
def business_metrics(request, filters: Query[DashboardFilters]):
    return get_business_metrics(
        club=request.club,
        date_from=filters.date_from,
        date_to=filters.date_to,
    )
