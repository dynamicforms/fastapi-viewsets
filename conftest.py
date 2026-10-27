"""
Django configuration for the whole test session. Django settings can be configured once per process,
so this configures the union of what every Django test needs before any test module is imported.
Skipped when Django is not installed.
"""

import atexit
import os
import tempfile


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

    django_settings.configure(
        SECRET_KEY="test-secret-key",  # noqa: S106 - not a real credential, just a test fixture
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.sessions",
            "fastapi_viewsets.backends",
        ],
        DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": path}},
        SESSION_ENGINE="django.contrib.sessions.backends.db",
        USE_TZ=True,
        DEBUG=True,
    )
    django.setup()

    from django.core.management import call_command

    call_command("migrate", run_syncdb=True, verbosity=0)


_configure_django()
