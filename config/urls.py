import posixpath
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_not_required
from django.http import FileResponse, Http404, HttpResponse
from django.urls import include, path, re_path
from django.utils import timezone
from django.utils.html import format_html, json_script
from django.views.static import serve

from config.api import api

api_urlpatterns, api_app_name, api_namespace = api.urls
DEBUG_PUBLIC_MEDIA_PREFIXES = ("club_logos/",)
DEBUG_MOCK_PAYMENT_TEMPLATE = """<!doctype html>
<html lang="ru">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Тестовая оплата СБП</title>
<style>
body{{font-family:system-ui;max-width:32rem;margin:4rem auto;padding:1rem}}
button{{min-height:44px;padding:.75rem 1rem}}
</style>
<h1>Тестовая оплата СБП</h1>
<p>Эта страница доступна только локальному mock-провайдеру.</p>
<button id="pay" type="button">Оплатить тестовый заказ</button>
<p id="status" role="status"></p>
{}{}
<script>
const payload=JSON.parse(document.getElementById('mock-payload').textContent);
const returnUrl=JSON.parse(document.getElementById('mock-return').textContent);
document.getElementById('pay').addEventListener('click',async()=>{{
  const status=document.getElementById('status');
  status.textContent='Подтверждаем…';
  try{{
    const response=await fetch('/api/billing/payment-provider-webhooks/mock/',{{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify(payload)
    }});
    if(!response.ok)throw new Error('mock payment failed');
    window.location.assign(returnUrl);
  }}catch{{
    status.textContent='Не удалось подтвердить тестовую оплату';
  }}
}});
</script>
</html>"""


def admin_sw_view(request):
    """Serve admin-sw.js under /dashboard/ scope with correct headers."""
    sw_path = Path(settings.BASE_DIR) / "static" / "js" / "admin-sw.js"
    response = FileResponse(open(sw_path, "rb"), content_type="application/javascript")
    response["Service-Worker-Allowed"] = "/dashboard/"
    return response


def _mark_patterns_public(urlpatterns):
    """Mark URL pattern callbacks as login_not_required.

    Django 5.2's LoginRequiredMiddleware checks ``view_func.login_required``.
    API views and debug-served public media should bypass that middleware.
    """
    for pattern in urlpatterns:
        if hasattr(pattern, "callback") and pattern.callback:
            pattern.callback.login_required = False
            pattern.callback = login_not_required(pattern.callback)
        if hasattr(pattern, "url_patterns"):
            _mark_patterns_public(pattern.url_patterns)
    return urlpatterns


def debug_public_media_view(request, path: str):
    """Serve only public-by-design media in DEBUG."""
    normalized_path = posixpath.normpath(path.replace("\\", "/")).lstrip("/")
    if (
        normalized_path in {"", "."}
        or normalized_path.startswith("../")
        or not any(normalized_path.startswith(prefix) for prefix in DEBUG_PUBLIC_MEDIA_PREFIXES)
    ):
        raise Http404
    return serve(request, normalized_path, document_root=settings.MEDIA_ROOT)


def debug_mock_payment_view(request, payment_link_id: str):
    """Local-only mock checkout; hard-disabled for every live provider/runtime."""

    if (
        not settings.DEBUG
        or settings.PAYMENT_PROVIDER != "mock"
        or not settings.MOCK_PAYMENT_ORDER_CREATION_ENABLED
        or request.method != "GET"
        or not 1 <= len(payment_link_id) <= 120
    ):
        raise Http404
    from apps.billing.models import BankPaymentOrder, PaymentReturnState
    from apps.billing.service_modules.payment_returns import _hash

    order = BankPaymentOrder.objects.unscoped().filter(
        provider=BankPaymentOrder.Provider.MOCK,
        provider_payment_link_id=payment_link_id,
    ).first()
    return_url = str(request.GET.get("return_url") or "")
    parsed = urlsplit(return_url)
    allowed_origin = str(settings.JAGUAR_PAYMENT_RETURN_ORIGIN or "").rstrip("/")
    state_values = parse_qs(parsed.query).get("state", [])
    if (
        order is None
        or f"{parsed.scheme}://{parsed.netloc}" != allowed_origin
        or parsed.path != "/payments/return"
        or parsed.fragment
        or len(state_values) != 1
        or not PaymentReturnState.objects.filter(
            order=order,
            state_hash=_hash(state_values[0]),
            expires_at__gt=timezone.now(),
        ).exists()
    ):
        raise Http404
    payload = {
        "webhookType": "acquiringInternetPayment",
        "event_id": f"mock-checkout-{order.id}",
        "status": "approved",
        "paymentLinkId": order.provider_payment_link_id,
        "operationId": f"mock-operation-{order.id}",
        "amount": str(order.amount_snapshot),
        "paid_at": timezone.now().isoformat(),
    }
    body = format_html(
        DEBUG_MOCK_PAYMENT_TEMPLATE,
        json_script(payload, "mock-payload"),
        json_script(return_url, "mock-return"),
    )
    response = HttpResponse(body)
    response["Content-Security-Policy"] = (
        "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'"
    )
    response["Cache-Control"] = "no-store"
    return response


urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("_allauth/", include("allauth.headless.urls")),
    path(
        "api/",
        include((_mark_patterns_public(api_urlpatterns), api_app_name), namespace=api_namespace),
    ),
    path("dashboard/admin-sw.js", admin_sw_view, name="admin-sw"),
    path("dashboard/", include("apps.htmx_admin.urls")),
    path(
        "mock-payments/<str:payment_link_id>",
        login_not_required(debug_mock_payment_view),
        name="debug-mock-payment",
    ),
]

if settings.DEBUG:
    urlpatterns += _mark_patterns_public(
        [re_path(r"^media/(?P<path>.*)$", debug_public_media_view, name="debug-public-media")]
    )
