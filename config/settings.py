"""Pingota settings.

One process serves the public site, the captive portal and the admin panel.
Configuration comes from environment variables only (Dokploy injects them).
"""
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env(name, default=""):
    return os.environ.get(name, default)


def env_bool(name, default=False):
    return env(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def env_list(name, default=""):
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


# --- Core -------------------------------------------------------------------
SECRET_KEY = env("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = env_bool("DJANGO_DEBUG", False)
ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost,testserver")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

PUBLIC_BASE_URL = env("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
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
LOGIN_URL = "/admin/login/"
LOGIN_REDIRECT_URL = "/admin/"
LOGOUT_REDIRECT_URL = "/admin/login/"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
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
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "SAMEORIGIN"

# --- Hardening / limits -----------------------------------------------------
DATA_UPLOAD_MAX_MEMORY_SIZE = 2 * 1024 * 1024
CSRF_FAILURE_VIEW = "apps.web.admin_views.csrf_failure"

# --- Scheduler --------------------------------------------------------------
SCHEDULER_ENABLED = env_bool("SCHEDULER_ENABLED", True)
EXPIRY_TICK_SECONDS = int(env("EXPIRY_TICK_SECONDS", "30"))
SYNC_TICK_SECONDS = int(env("SYNC_TICK_SECONDS", "60"))

# --- Payments ---------------------------------------------------------------
# Paystack is the default rail (cards, mobile money, bank). M-Pesa Daraja is
# kept per-tenant for KES settlements straight to the ISP's own shortcode.
PAYSTACK_BASE_URL = env("PAYSTACK_BASE_URL", "https://api.paystack.co")
PAYSTACK_SECRET_KEY = env("PAYSTACK_SECRET_KEY", "")
PAYMENT_DEFAULT_CURRENCY = env("PAYMENT_DEFAULT_CURRENCY", "KES")
MPESA_ENV = env("MPESA_ENV", "sandbox")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"plain": {"format": "%(asctime)s %(levelname)s %(name)s: %(message)s"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "plain"}},
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
}
