from pathlib import Path

import environ
from corsheaders.defaults import default_headers

env = environ.Env()

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY")
DEBUG = env.bool("DEBUG", default=False)
ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])
REFRESH_COOKIE_SECURE = env.bool("REFRESH_COOKIE_SECURE", default=not DEBUG)

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sites",
    # Third-party
    "allauth",
    "allauth.account",
    "allauth.headless",
    "corsheaders",
    "django_htmx",
    "template_partials",
    "django_q",
    # Project apps
    "apps.common",
    "apps.clubs",
    "apps.students",
    "apps.trainers",
    "apps.attendance",
    "apps.billing",
    "apps.grades",
    "apps.notifications",
    "apps.dashboard",
    "apps.retention",
    "apps.documents",
    "apps.feedback",
    "apps.onboarding",
    "apps.leads",
    "apps.pipelines",
    "apps.htmx_admin",
]

SITE_ID = 1

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    "apps.common.middleware.TenantMiddleware",
    "apps.common.middleware.RequestLogMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
]

AUTHENTICATION_BACKENDS = [
    "django.contrib.auth.backends.ModelBackend",
    "allauth.account.auth_backends.AuthenticationBackend",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.common.context_processors.club_branding",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {"default": env.db("DATABASE_URL")}

REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/1")
REDIS_CACHE_URL = env("REDIS_CACHE_URL", default=REDIS_URL)
REDIS_Q_URL = env("REDIS_Q_URL", default=REDIS_URL)

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.redis.RedisCache",
        "LOCATION": REDIS_CACHE_URL,
        "KEY_PREFIX": env("REDIS_CACHE_KEY_PREFIX", default="jaguar-crm"),
    }
}

# allauth
ACCOUNT_LOGIN_METHODS = {"email", "username"}
ACCOUNT_SIGNUP_FIELDS = ["username*", "email", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "optional"

# allauth headless
HEADLESS_ONLY = False
HEADLESS_TOKEN_STRATEGY = "apps.common.auth.TenantJWTTokenStrategy"

HEADLESS_FRONTEND_URLS = {
    "account_confirm_email": env("FRONTEND_URL", default="http://localhost:5173") + "/verify-email/{key}",
    "account_reset_password_from_key": env("FRONTEND_URL", default="http://localhost:5173") + "/reset-password/{key}",
}

# JWT settings (allauth headless)
HEADLESS_JWT_ACCESS_TOKEN_EXPIRES_IN = 900
HEADLESS_JWT_REFRESH_TOKEN_EXPIRES_IN = 604800
HEADLESS_JWT_ROTATE_REFRESH_TOKEN = True
_jwt_key_path = BASE_DIR / "jwt-key.pem"
_jwt_private_key_env = env("JWT_PRIVATE_KEY", default="")
HEADLESS_JWT_PRIVATE_KEY = (
    _jwt_private_key_env.replace("\\n", "\n")
    if _jwt_private_key_env
    else _jwt_key_path.read_text()
    if _jwt_key_path.exists()
    else ""
)

# django-q2
Q_CLUSTER = {
    "name": "jaguar",
    "workers": 4,
    "timeout": 120,
    "retry": 180,
    "redis": REDIS_Q_URL,
}

# CORS
CORS_ALLOWED_ORIGINS = env.list("CORS_ALLOWED_ORIGINS", default=["http://localhost:5173"])
CORS_ALLOW_HEADERS = (*default_headers, "x-request-id")

# Public landing lead intake
LANDING_DEFAULT_CLUB_ID = env.int("LANDING_DEFAULT_CLUB_ID", default=0)
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_LEAD_CHAT_ID = env("TELEGRAM_LEAD_CHAT_ID", default="")
TELEGRAM_LEAD_MESSAGE_THREAD_ID = env.int("TELEGRAM_LEAD_MESSAGE_THREAD_ID", default=0)
TELEGRAM_LEAD_MESSAGE_MODE = env("TELEGRAM_LEAD_MESSAGE_MODE", default="full")
CRM_PUBLIC_BASE_URL = env("CRM_PUBLIC_BASE_URL", default="")

# Online payment links
PAYMENT_PROVIDER = env("PAYMENT_PROVIDER", default="mock")
ONLINE_PAYMENT_ORDER_CREATION_ENABLED = env.bool(
    "ONLINE_PAYMENT_ORDER_CREATION_ENABLED",
    default=False,
)
MOCK_PAYMENT_WEBHOOKS_ENABLED = env.bool(
    "MOCK_PAYMENT_WEBHOOKS_ENABLED",
    default=False,
)
MOCK_PAYMENT_ORDER_CREATION_ENABLED = env.bool(
    "MOCK_PAYMENT_ORDER_CREATION_ENABLED",
    default=False,
)
TOCHKA_PAYMENT_RECONCILIATION_ENABLED = env.bool(
    "TOCHKA_PAYMENT_RECONCILIATION_ENABLED",
    default=False,
)
TOCHKA_FISCALIZATION_READY = env.bool(
    "TOCHKA_FISCALIZATION_READY",
    default=False,
)
TOCHKA_FISCALIZATION_DECISION_ID = env("TOCHKA_FISCALIZATION_DECISION_ID", default="")
TOCHKA_RECONCILIATION_COOLDOWN_SECONDS = env.int(
    "TOCHKA_RECONCILIATION_COOLDOWN_SECONDS",
    default=60,
)
TOCHKA_RECONCILIATION_MAX_ATTEMPTS = env.int(
    "TOCHKA_RECONCILIATION_MAX_ATTEMPTS",
    default=8,
)
STUDENT_OPENING_IMPORT_ENABLED = env.bool("STUDENT_OPENING_IMPORT_ENABLED", default=False)
STUDENT_ADMIN_CORRECTIONS_ENABLED = env.bool("STUDENT_ADMIN_CORRECTIONS_ENABLED", default=False)
TRAINER_SETTLEMENTS_ENABLED = env.bool("TRAINER_SETTLEMENTS_ENABLED", default=False)
MANUAL_OPERATIONAL_ADMISSION_ENABLED = env.bool(
    "MANUAL_OPERATIONAL_ADMISSION_ENABLED",
    default=False,
)
TRAINING_GROUP_NEW_WRITES_ENABLED = env.bool(
    "TRAINING_GROUP_NEW_WRITES_ENABLED",
    default=False,
)
UNIFIED_CLIENT_JOURNEY_ENABLED = env.bool(
    "UNIFIED_CLIENT_JOURNEY_ENABLED",
    default=False,
)
MOCK_PAYMENT_BASE_URL = env("MOCK_PAYMENT_BASE_URL", default="http://localhost:8000")
TOCHKA_API_BASE_URL = env("TOCHKA_API_BASE_URL", default="https://enter.tochka.com/uapi")
TOCHKA_JWT_TOKEN = env("TOCHKA_JWT_TOKEN", default="")
TOCHKA_CLIENT_ID = env("TOCHKA_CLIENT_ID", default="")
TOCHKA_CUSTOMER_CODE = env("TOCHKA_CUSTOMER_CODE", default="")
TOCHKA_MERCHANT_ID = env("TOCHKA_MERCHANT_ID", default="")
TOCHKA_PAYMENT_MODES = env.list("TOCHKA_PAYMENT_MODES", default=["sbp"])
TOCHKA_RETAILER_READBACK_MAX_AGE_SECONDS = env.int(
    "TOCHKA_RETAILER_READBACK_MAX_AGE_SECONDS",
    default=3600,
)
TOCHKA_RETAILER_READBACK_AUTO_REFRESH_ENABLED = env.bool(
    "TOCHKA_RETAILER_READBACK_AUTO_REFRESH_ENABLED",
    default=False,
)
TOCHKA_WEBHOOK_KEY_MODE = env("TOCHKA_WEBHOOK_KEY_MODE", default="official_jwk")
TOCHKA_WEBHOOK_PUBLIC_KEY = env("TOCHKA_WEBHOOK_PUBLIC_KEY", default="").replace("\\n", "\n")
TOCHKA_WEBHOOK_JWK_CACHE_SECONDS = env.int("TOCHKA_WEBHOOK_JWK_CACHE_SECONDS", default=3600)
TOCHKA_WEBHOOK_JWK_LKG_SECONDS = env.int("TOCHKA_WEBHOOK_JWK_LKG_SECONDS", default=86400)
TOCHKA_WEBHOOK_JWK_REFRESH_COOLDOWN_SECONDS = env.int(
    "TOCHKA_WEBHOOK_JWK_REFRESH_COOLDOWN_SECONDS",
    default=60,
)
TOCHKA_WEBHOOK_MAX_BODY_BYTES = env.int("TOCHKA_WEBHOOK_MAX_BODY_BYTES", default=16384)
TOCHKA_SUCCESS_REDIRECT_URL = env("TOCHKA_SUCCESS_REDIRECT_URL", default="")
TOCHKA_FAIL_REDIRECT_URL = env("TOCHKA_FAIL_REDIRECT_URL", default="")
TOCHKA_STAFF_PAYMENT_LINK_TTL_MINUTES = env.int("TOCHKA_STAFF_PAYMENT_LINK_TTL_MINUTES", default=10080)
TOCHKA_SELF_SERVICE_PAYMENT_LINK_TTL_MINUTES = env.int("TOCHKA_SELF_SERVICE_PAYMENT_LINK_TTL_MINUTES", default=4320)
TOCHKA_RECEIPT_MODE = env("TOCHKA_RECEIPT_MODE", default="none")
TOCHKA_RECEIPT_TAX_SYSTEM_CODE = env("TOCHKA_RECEIPT_TAX_SYSTEM_CODE", default="")
TOCHKA_RECEIPT_VAT_TYPE = env("TOCHKA_RECEIPT_VAT_TYPE", default="")
TOCHKA_RECEIPT_PAYMENT_METHOD = env("TOCHKA_RECEIPT_PAYMENT_METHOD", default="")
TOCHKA_REQUEST_TIMEOUT_SECONDS = env.int("TOCHKA_REQUEST_TIMEOUT_SECONDS", default=15)
JAGUAR_PAYMENT_RETURN_ORIGIN = env("JAGUAR_PAYMENT_RETURN_ORIGIN", default="")
JAGUAR_PAYMENT_RETURN_ALLOWED_ORIGIN = "https://app.jaguar-fight-club.ru"

# Static files
STATIC_URL = "/static/"
STATICFILES_DIRS = [
    ("admin", BASE_DIR / "static" / "admin"),
    ("css", BASE_DIR / "static" / "css"),
    ("js", BASE_DIR / "static" / "js"),
    ("vendor", BASE_DIR / "static" / "vendor"),
]
STATIC_ROOT = BASE_DIR / "staticfiles"

LOGIN_URL = "/dashboard/login/"

# Media files (user uploads)
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
FILE_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10MB
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024 * 1024  # 10MB

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Localization
LANGUAGE_CODE = "ru"
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_L10N = True
USE_TZ = True

# Logging
LOG_LEVEL = env("LOG_LEVEL", default="INFO")
DJANGO_LOG_LEVEL = env("DJANGO_LOG_LEVEL", default="WARNING")
LOG_FORMAT = env("LOG_FORMAT", default="json")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name} {message}",
            "style": "{",
            "()": "apps.common.logging.SafeTextFormatter",
        },
        "json": {
            "()": "apps.common.logging.JsonLogFormatter",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "json" if LOG_FORMAT == "json" else "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": LOG_LEVEL,
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": DJANGO_LOG_LEVEL,
            "propagate": False,
        },
        "django.request": {
            "handlers": ["console"],
            "level": "ERROR",
            "propagate": False,
        },
        "django.security": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
        "django.server": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "apps": {
            "handlers": ["console"],
            "level": LOG_LEVEL,
            "propagate": False,
        },
    },
}

# Web Push (VAPID)
VAPID_PRIVATE_KEY = env("VAPID_PRIVATE_KEY", default="")
VAPID_PUBLIC_KEY = env("VAPID_PUBLIC_KEY", default="")
VAPID_ADMIN_EMAIL = env("VAPID_ADMIN_EMAIL", default="admin@example.com")
