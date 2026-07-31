import os
import base64
from pathlib import Path

import environ
from django.core.exceptions import ImproperlyConfigured

BASE_DIR = Path(__file__).resolve().parent.parent

env = environ.Env(DEBUG=(bool, False))
environ.Env.read_env(BASE_DIR / ".env")

development_default_key = "django-insecure-36m0b016f(m^5&0)s-@de=v&wmxwot^o%ts3!5f@fbbrw$jf8&"
SECRET_KEY = env("SECRET_KEY", default=development_default_key)
DEBUG = env.bool("DEBUG", default=False)

if not DEBUG and (not SECRET_KEY or SECRET_KEY == development_default_key):
    raise ImproperlyConfigured("SECRET_KEY must be explicitly configured in production.")

ALLOWED_HOSTS = env.list("ALLOWED_HOSTS", default=[])
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])

if not DEBUG:
    if not ALLOWED_HOSTS:
        raise ImproperlyConfigured("ALLOWED_HOSTS must be explicitly configured when DEBUG=False.")
else:
    if not ALLOWED_HOSTS:
        ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "accounts",
    "billing",
    "analytics",
    "ai_engine",
    "dashboard",
    "reports",
    "simulator",
    "remediation",
    "scheduler",
    "oci_connector",
    "api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "cloud_cost_detective.urls"

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
                "accounts.context_processors.active_project_processor",
            ],
        },
    },
]

WSGI_APPLICATION = "cloud_cost_detective.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": env("DB_ENGINE", default="django.db.backends.sqlite3"),
        "NAME": env("DB_NAME", default=str(BASE_DIR / "db.sqlite3")),
        "USER": env("DB_USER", default=""),
        "PASSWORD": env("DB_PASSWORD", default=""),
        "HOST": env("DB_HOST", default="localhost"),
        "PORT": env("DB_PORT", default="5432"),
    }
}

if not DEBUG:
    engine = DATABASES["default"]["ENGINE"]
    if engine != "django.db.backends.postgresql":
        raise ImproperlyConfigured(f"Production database engine must be PostgreSQL, got '{engine}'.")
    for key in ["NAME", "USER", "PASSWORD", "HOST"]:
        if not DATABASES["default"].get(key):
            raise ImproperlyConfigured(f"Production database setting '{key}' must be explicitly configured.")

AUTH_USER_MODEL = "accounts.User"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard-home"
LOGOUT_REDIRECT_URL = "login"


CELERY_BROKER_URL = env("CELERY_BROKER_URL", default="redis://localhost:6379/0")
CELERY_RESULT_BACKEND = env("CELERY_RESULT_BACKEND", default="redis://localhost:6379/0")

# Gemini API Configuration
GEMINI_API_KEY = env("GEMINI_API_KEY", default="")
GEMINI_MODEL = env("GEMINI_MODEL", default="gemini-2.5-flash")

# OCI Integration Configuration
OCI_ENCRYPTION_KEY = env("OCI_ENCRYPTION_KEY", default="")

if not DEBUG:
    if not OCI_ENCRYPTION_KEY:
        raise ImproperlyConfigured("OCI_ENCRYPTION_KEY must be configured in production.")
    try:
        key_bytes = base64.urlsafe_b64decode(OCI_ENCRYPTION_KEY)
        if len(key_bytes) != 32:
            raise ValueError()
    except Exception:
        raise ImproperlyConfigured("OCI_ENCRYPTION_KEY must be a valid 32-byte URL-safe base64 key.")

# Custom Test Runner for automatic tenancy backfilling in legacy tests
TEST_RUNNER = "cloud_cost_detective.test_runner.CustomDiscoverRunner"

SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=not DEBUG)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=not DEBUG)
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=False)
SECURE_HSTS_SECONDS = env.int("SECURE_HSTS_SECONDS", default=0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = env.bool("SECURE_HSTS_INCLUDE_SUBDOMAINS", default=False)
SECURE_HSTS_PRELOAD = env.bool("SECURE_HSTS_PRELOAD", default=False)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "{levelname} {asctime} {module} {process:d} {thread:d} {message}",
            "style": "{",
        },
        "simple": {
            "format": "{levelname} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "level": "INFO",
            "class": "logging.StreamHandler",
            "formatter": "verbose" if not DEBUG else "simple",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
    },
}

CELERY_BEAT_SCHEDULE = {
    "sync-active-oci-connections-daily": {
        "task": "oci_connector.tasks.sync_all_active_connections_task",
        "schedule": 86400.0,
    },
}

