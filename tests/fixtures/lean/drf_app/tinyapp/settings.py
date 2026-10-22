"""Settings of the synthetic Django + DRF project used by the lean tests (not a real app)."""

import os

if os.environ.get("PD_TEST_BANNER"):
    # a banner written straight to file descriptor 1, past sys.stdout
    os.write(1, b"settings banner on stdout\n")

SECRET_KEY = "pd-test-only"
DEBUG = False
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"c": {"class": "logging.StreamHandler"}},
    "loggers": {"django.request": {"handlers": ["c"], "level": "ERROR"}},
}
ALLOWED_HOSTS = ["*"]
ROOT_URLCONF = "tinyapp.urls"
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.admin",
    "rest_framework",
    "tinyapp",
]
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("APP_DB", ":memory:"),
    }
}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"
USE_TZ = True
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [],
    "DEFAULT_PERMISSION_CLASSES": [],
    "UNAUTHENTICATED_USER": None,
}
