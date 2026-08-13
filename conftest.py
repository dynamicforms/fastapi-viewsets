"""
Django configuration for the whole test session.

Django settings can be configured exactly once per process, so two test modules that each configure
what they alone need will fight: whichever imports first wins, and the other runs against an
INSTALLED_APPS that is missing its models. This does it once, up front, with the union of what
every Django-touching test needs - conftest is imported before any test module, so the modules'
own `if configured: return` guards see it is already done and stay out of the way.

Skipped entirely when Django is not installed; it is an optional extra.
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

    # A file rather than ":memory:". Django keeps one connection per thread, and the async ORM
    # calls in backends/django_orm.py run on worker threads - against an in-memory database each
    # would open its own, empty one and every query would fail on a missing table.
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
