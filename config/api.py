import redis
from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.db import connection
from ninja import NinjaAPI
from ninja.throttling import AnonRateThrottle, AuthRateThrottle

from apps.common.auth import TenantJWTAuth
from apps.common.exceptions import BusinessLogicError, TenantAccessError

api = NinjaAPI(
    title="CRM Jaguar API",
    version="1.0.0",
    auth=[TenantJWTAuth()],
    throttle=[AnonRateThrottle("10/s"), AuthRateThrottle("100/s")],
    docs_url="/docs/" if settings.DEBUG else None,
    openapi_url="/openapi.json" if settings.DEBUG else None,
)


@api.exception_handler(BusinessLogicError)
def on_business_error(request, exc):
    payload = {"detail": exc.message, "code": exc.code}
    safe_payload = getattr(exc, "safe_payload", None)
    if isinstance(safe_payload, dict):
        payload.update(
            {
                key: value
                for key, value in safe_payload.items()
                if key not in {"detail", "code"}
            }
        )
    return api.create_response(request, payload, status=400)


@api.exception_handler(TenantAccessError)
def on_tenant_denied(request, exc):
    return api.create_response(request, {"detail": "Access denied"}, status=403)


@api.exception_handler(ObjectDoesNotExist)
def on_not_found(request, exc):
    return api.create_response(request, {"detail": "Not found"}, status=404)


api.add_router("/clubs/", "apps.clubs.api.router")
api.add_router("/auth/", "apps.common.auth_api.router")
api.add_router("/students/", "apps.students.api.router")
api.add_router("/trainers/", "apps.trainers.api.router")
api.add_router("/schedules/", "apps.attendance.api.router")
api.add_router("/guest-bookings/", "apps.attendance.api.guest_booking_router")
api.add_router("/personal-bookings/", "apps.attendance.api.personal_booking_router")
api.add_router("/personal-availability/", "apps.attendance.api.personal_availability_router")
api.add_router("/personal-drop-in-bookings/", "apps.attendance.api.personal_drop_in_router")
api.add_router("/checkins/", "apps.attendance.api.checkin_router")
api.add_router("/billing/", "apps.billing.api.router")
api.add_router("/grades/", "apps.grades.api.router")
api.add_router("/notifications/", "apps.notifications.api.router")
api.add_router("/dashboard/", "apps.dashboard.api.router")
api.add_router("/retention/", "apps.retention.api.router")
api.add_router("/parents/", "apps.students.parent_api.router")
api.add_router("/documents/", "apps.documents.api.router")
api.add_router("/feedback/", "apps.feedback.api.router")
api.add_router("/onboarding/", "apps.onboarding.api.router")
api.add_router("/public/lead-intakes/", "apps.leads.public_api.router")
api.add_router("/leads/", "apps.leads.api.router")
api.add_router("/pipelines/", "apps.pipelines.api.router")


@api.get("/health/", auth=None)
def health(request):
    return {"status": "healthy"}


def _ready_check_database() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return True


def _ready_check_cache() -> bool:
    key = "readiness"
    cache.set(key, "ok", timeout=10)
    return cache.get(key) == "ok"


def _ready_check_queue_redis() -> bool:
    client = redis.Redis.from_url(
        settings.Q_CLUSTER["redis"],
        socket_connect_timeout=1,
        socket_timeout=1,
    )
    return bool(client.ping())


@api.get("/ready/", auth=None)
def ready(request):
    checks: dict[str, str] = {}
    for name, check in (
        ("database", _ready_check_database),
        ("cache", _ready_check_cache),
        ("queue", _ready_check_queue_redis),
    ):
        try:
            checks[name] = "ok" if check() else "error"
        except Exception:
            checks[name] = "error"
    status = "ready" if all(value == "ok" for value in checks.values()) else "degraded"
    return api.create_response(
        request,
        {"status": status, "checks": checks},
        status=200 if status == "ready" else 503,
    )
