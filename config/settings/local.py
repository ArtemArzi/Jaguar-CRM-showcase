from .base import *  # noqa: F401, F403

DEBUG = True
ALLOWED_HOSTS = ["*"]
ACCOUNT_EMAIL_VERIFICATION = "none"
REFRESH_COOKIE_SECURE = env.bool("REFRESH_COOKIE_SECURE", default=False)  # noqa: F405

# Public demo tunnel: fxTunnel.
CSRF_TRUSTED_ORIGINS = [
    "https://*.fxtun.dev",
    "https://*.fxtun.ru",
]
