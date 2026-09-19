from .base import *  # noqa: F401, F403

DEBUG = True
ALLOWED_HOSTS = ["*"]
ACCOUNT_EMAIL_VERIFICATION = "none"
ONLINE_PAYMENT_ORDER_CREATION_ENABLED = True
MOCK_PAYMENT_WEBHOOKS_ENABLED = True
MOCK_PAYMENT_ORDER_CREATION_ENABLED = True
TOCHKA_PAYMENT_RECONCILIATION_ENABLED = True
TOCHKA_FISCALIZATION_READY = True
UNIFIED_CLIENT_JOURNEY_ENABLED = False
TOCHKA_FISCALIZATION_DECISION_ID = "synthetic-test-decision"
TOCHKA_PAYMENT_MODES = ["sbp"]
TOCHKA_RETAILER_READBACK_MAX_AGE_SECONDS = 3600
JAGUAR_PAYMENT_RETURN_ORIGIN = "https://app.jaguar.test"
JAGUAR_PAYMENT_RETURN_ALLOWED_ORIGIN = "https://app.jaguar.test"
TOCHKA_WEBHOOK_KEY_MODE = "pem"

# Use SQLite for tests when PostgreSQL is unavailable
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Disable cache backend that requires Redis
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}
