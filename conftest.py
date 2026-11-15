"""
Django configuration for the whole test session. Django settings can be configured once per process,
so this configures the union of what every Django test needs before any test module is imported.
Skipped when Django is not installed.

With `FASTAPI_VIEWSETS_TEST_POSTGRES_URL` set (`postgres://user:password@host:port/any`), Django also gets the
alias `adorm`: the PostgreSQL database `adorm_fastapi_viewsets_test` on that server, which the adorm backend's
tests create, use and drop.
"""

import atexit
import os
import tempfile

from urllib.parse import unquote, urlsplit

POSTGRES_URL_VARIABLE = "FASTAPI_VIEWSETS_TEST_POSTGRES_URL"
ADORM_DATABASE_ALIAS = "adorm"
ADORM_DATABASE_NAME = "adorm_fastapi_viewsets_test"


def _postgres_database(url: str) -> dict:
    """The Django `DATABASES` entry of the database `ADORM_DATABASE_NAME` on the server of `url`."""
    parts = urlsplit(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": ADORM_DATABASE_NAME,
        "USER": unquote(parts.username or ""),
        "PASSWORD": unquote(parts.password or ""),
        "HOST": parts.hostname or "",
        "PORT": str(parts.port or ""),
    }


class _AdormModelsRouter:
    """Sends the queries Django runs itself for the adorm tests' models (`backends.Adorm*`) to the `adorm` alias."""

    def db_for_read(self, model, **_hints):
        return ADORM_DATABASE_ALIAS if model._meta.label.startswith("backends.Adorm") else None

    db_for_write = db_for_read


def _configure_django() -> None:
    try:
        import django
    except ImportError:  # pragma: no cover - the django extra is not installed
        return

    from django.conf import settings as django_settings

    if django_settings.configured:  # pragma: no cover - only if something configured even earlier
        return

    # A file, not ":memory:": the async ORM runs on worker threads, each with its own connection,
    # and an in-memory database would be empty for each of them.
    handle, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(handle)
    atexit.register(lambda: os.path.exists(path) and os.unlink(path))

    databases = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": path}}
    routers = []
    if os.environ.get(POSTGRES_URL_VARIABLE):
        databases[ADORM_DATABASE_ALIAS] = _postgres_database(os.environ[POSTGRES_URL_VARIABLE])
        routers.append(_AdormModelsRouter())

    django_settings.configure(
        SECRET_KEY="test-secret-key",  # noqa: S106 - not a real credential, just a test fixture
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.sessions",
            "fastapi_viewsets.backends",
        ],
        DATABASES=databases,
        DATABASE_ROUTERS=routers,
        SESSION_ENGINE="django.contrib.sessions.backends.db",
        USE_TZ=True,
        DEBUG=True,
    )
    django.setup()

    from django.core.management import call_command

    call_command("migrate", run_syncdb=True, verbosity=0)


_configure_django()
