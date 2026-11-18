"""Pingota settings.

One process serves the public site, the captive portal and the admin panel.
Configuration comes from environment variables only (Dokploy injects them).
"""
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")

TESTING = any(argument == "test" for argument in sys.argv)


def env(name, default=""):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


# --- Core -------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

# Loopback addresses (127.0.0.1, localhost) must always be allowed so container
# health checks (e.g. docker-compose healthcheck on http://127.0.0.1:8000/healthz/)
# succeed even when DJANGO_ALLOWED_HOSTS only lists the public domain name.
_configured_hosts = env_list("DJANGO_ALLOWED_HOSTS")
_always_allowed = ["127.0.0.1", "localhost", "testserver"]
if PUBLIC_BASE_URL:
    try:
        from urllib.parse import urlparse

        _pub_host = urlparse(PUBLIC_BASE_URL).hostname
        if _pub_host and _pub_host not in _always_allowed and _pub_host not in _configured_hosts:
            _configured_hosts.append(_pub_host)
    except Exception:
        pass
ALLOWED_HOSTS = list(dict.fromkeys(_always_allowed + _configured_hosts))

_csrf_origins = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
if PUBLIC_BASE_URL and PUBLIC_BASE_URL.startswith(("http://", "https://")) and PUBLIC_BASE_URL not in _csrf_origins:
    _csrf_origins.append(PUBLIC_BASE_URL)
CSRF_TRUSTED_ORIGINS = _csrf_origins

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
# Forwarded client IP headers are only read from a private/loopback proxy peer.
# Set TRUSTED_PROXY_IPS when the reverse proxy itself has a public address.
TRUST_PROXY_HEADERS = env_bool("TRUST_PROXY_HEADERS", True)
TRUSTED_PROXY_IPS = env_list("TRUSTED_PROXY_IPS")

# The panel is multi-tenant: each ISP logs in and sees only their own data.
# PORTAL_SLUG is used only by the first-run bootstrap and the demo seed.
PORTAL_SLUG = env("PORTAL_SLUG", "default")

# Router onboarding uses TunGuard's peer API and WireGuard-compatible tunnel.
ROUTER_TUNNEL_HOST = env("ROUTER_TUNNEL_HOST", "vpn.pingota.example")
ROUTER_TUNNEL_PORT = int(env("ROUTER_TUNNEL_PORT", "13231"))
ROUTER_TUNNEL_SUBNET = env("ROUTER_TUNNEL_SUBNET", "10.70.0.0/16")
TUNGARD_API_URL = env("TUNGARD_API_URL", "")
TUNGARD_API_KEY = env("TUNGARD_API_KEY", "")

# --- Database ---------------------------------------------------------------
# MongoDB is the intended store: tenant documents, payment records and the
# per-minute router samples all index cleanly on it, and the hosted product can
# point many small ISPs at one cluster. SQLite keeps a laptop install trivial.
DATA_DIR = BASE_DIR / "data"
MONGODB_URI = env("MONGODB_URI")
MONGODB_NAME = env("MONGODB_NAME", "pingota")
DATABASE_URL = env("DATABASE_URL")

if MONGODB_URI:
    DATABASES = {
        "default": {
            "ENGINE": "django_mongodb_backend",
            "NAME": MONGODB_NAME,
            "HOST": MONGODB_URI,
        }
    }
    # App models keep UUID keys everywhere, so migrations are identical on both
    # engines. Django's own tables (users, sessions, admin log) are created by
    # the same migrations either way.
elif DATABASE_URL and DATABASE_URL.startswith("postgres"):
    from urllib.parse import urlparse

    parsed = urlparse(DATABASE_URL)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",  # optional, needs psycopg
            "NAME": parsed.path.lstrip("/"),
            "USER": parsed.username or "",
            "PASSWORD": parsed.password or "",
            "HOST": parsed.hostname or "",
            "PORT": parsed.port or "",
            "CONN_MAX_AGE": 600,
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": BASE_DIR / "data" / "pingota.sqlite3",
        }
    }
    DATA_DIR.mkdir(exist_ok=True)

# --- Cache ------------------------------------------------------------------
# Rate limits and sign-in lockouts must be shared by every worker. Set REDIS_URL
# in production (compose provides redis://redis:6379/0); a laptop keeps the
# in-process cache, where one worker is the whole deployment anyway.
REDIS_URL = env("REDIS_URL", "")
if REDIS_URL:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            "LOCATION": REDIS_URL,
            "KEY_PREFIX": "pingota",
            "TIMEOUT": 300,
        }
    }
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
            "LOCATION": "pingota",
            "TIMEOUT": 300,
        }
    }

# --- Apps -------------------------------------------------------------------
INSTALLED_APPS = [
    "config.apps.MongoAdminConfig",
    "config.apps.MongoAuthConfig",
    "config.apps.MongoContentTypesConfig",
    "config.apps.MongoSessionsConfig",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "apps.accounts",
    "apps.tenants",
    "apps.network",
    "apps.billing",
    "apps.web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "config.middleware.SecurityHeadersMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "config.middleware.PlatformAdminGuardMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.web.context.branding",
            ],
        },
    }
]

AUTH_USER_MODEL = "auth.User"
AUTHENTICATION_BACKENDS = [
    "apps.accounts.backends.CaseInsensitiveEmailOrUsernameBackend",
]
LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/admin/"
LOGOUT_REDIRECT_URL = "/admin/login/"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-ke"
TIME_ZONE = env("TIME_ZONE", "Africa/Nairobi")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STATICFILES_DIRS = [BASE_DIR / "static"]

# Only the two webfonts are remote; everything else is same-origin.
CSP_STYLE_SRC = env_list("CSP_STYLE_SRC", "'self',https://fonts.googleapis.com")
CSP_FONT_SRC = env_list("CSP_FONT_SRC", "'self',https://fonts.gstatic.com,data:")
# The manifest backend needs collectstatic first, which runserver and tests do
# not do. Production (DEBUG off) gets hashed, compressed files.
STATICFILES_STORAGE = (
    "django.contrib.staticfiles.storage.StaticFilesStorage"
    if DEBUG
    else "whitenoise.storage.CompressedManifestStaticFilesStorage"
)
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": STATICFILES_STORAGE},
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- Product configuration --------------------------------------------------
# Encryption key for stored router credentials and payment secrets. Any long
# string; changing it invalidates stored secrets (re-enter them in Settings).
FIELD_ENCRYPTION_KEY = env("FIELD_ENCRYPTION_KEY", "")

SESSION_COOKIE_SECURE = env_bool("COOKIE_SECURE", not DEBUG)
CSRF_COOKIE_SECURE = env_bool("COOKIE_SECURE", not DEBUG)
SESSION_COOKIE_HTTPONLY = True
CSRF_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_AGE = int(env("SESSION_COOKIE_AGE", str(60 * 60 * 12)))
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
X_FRAME_OPTIONS = "DENY"

# --- Hardening / limits -----------------------------------------------------
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
CSRF_FAILURE_VIEW = "apps.web.admin_views.csrf_failure"
# Rate limits and lockouts stay on in every real deployment; they are skipped
# while the test suite runs so unrelated tests do not spend a shared budget.
SECURITY_LIMITS_ENABLED = env_bool("SECURITY_LIMITS_ENABLED", not TESTING)
# A site that is not configured for HTTPS must not redirect to it: local
# compose runs serve plain HTTP on localhost. Production sets PUBLIC_BASE_URL
# to the https:// domain, which turns the redirect on without extra flags.
SECURE_SSL_REDIRECT = env_bool(
    "SECURE_SSL_REDIRECT",
    not DEBUG and not TESTING and PUBLIC_BASE_URL.startswith("https://"),
)
SECURE_REDIRECT_EXEMPT = [r"^healthz/?$", r"^health/?$"]
SECURE_HSTS_SECONDS = int(
    env("SECURE_HSTS_SECONDS", "0" if (DEBUG or TESTING) else "31536000")
)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool(
    "SECURE_HSTS_INCLUDE_SUBDOMAINS", not DEBUG and not TESTING
)
SECURE_HSTS_PRELOAD = env_bool("SECURE_HSTS_PRELOAD", not DEBUG and not TESTING)

# --- Scheduler --------------------------------------------------------------
SCHEDULER_ENABLED = env_bool("SCHEDULER_ENABLED", True)
EXPIRY_TICK_SECONDS = int(env("EXPIRY_TICK_SECONDS", "30"))
SYNC_TICK_SECONDS = int(env("SYNC_TICK_SECONDS", "60"))

# --- Payments ---------------------------------------------------------------
# Paystack is the default rail (cards, mobile money, bank). M-Pesa Daraja is
# kept per-tenant for KES settlements straight to the ISP's own shortcode.
PAYSTACK_BASE_URL = env("PAYSTACK_BASE_URL", "https://api.paystack.co")
PAYSTACK_SECRET_KEY = env("PAYSTACK_SECRET_KEY", "")
# Paystack's current webhook signing secret (whsec_...). Per-ISP secrets live in
# PaystackConfig; this is the fallback for the platform account.
PAYSTACK_WEBHOOK_SECRET = env("PAYSTACK_WEBHOOK_SECRET", "")
PAYMENT_DEFAULT_CURRENCY = env("PAYMENT_DEFAULT_CURRENCY", "KES")
MPESA_ENV = env("MPESA_ENV", "sandbox")
# Platform till for hosting-plan payments. Guest pass sales use each ISP's own
# MpesaConfig; these credentials are deliberately separate.
MPESA_PLATFORM_SHORTCODE = env("MPESA_PLATFORM_SHORTCODE", "")
MPESA_PLATFORM_CONSUMER_KEY = env("MPESA_PLATFORM_CONSUMER_KEY", "")
MPESA_PLATFORM_CONSUMER_SECRET = env("MPESA_PLATFORM_CONSUMER_SECRET", "")
MPESA_PLATFORM_PASSKEY = env("MPESA_PLATFORM_PASSKEY", "")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
