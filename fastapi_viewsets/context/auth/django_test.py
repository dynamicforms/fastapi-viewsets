"""
Exercises DjangoSessionAuthBackend against a real (if minimal) Django app: an actual
django.contrib.sessions session store and django.contrib.auth user, not mocks - so the auth-hash
invalidation-on-password-change behaviour (a real Django security feature) is genuinely verified.

Requires the `django` extra (`pip install "dynamicforms-fastapi-viewsets[django]"`).
"""

import os
import tempfile

import pytest

django = pytest.importorskip("django")

from fastapi import Request  # noqa: E402

from fastapi_viewsets.context.auth.django import (  # noqa: E402
    _DjangoSessionUserLazy,
    DjangoSessionAuthBackend,
    DjangoSessionCookieAuthBackend,
)


def _configure_django():
    from django.conf import settings as django_settings

    if django_settings.configured:
        return

    db_fd, db_path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(db_fd)

    django_settings.configure(
        SECRET_KEY="test-secret-key",  # noqa: S106 - not a real credential, just a test fixture
        INSTALLED_APPS=[
            "django.contrib.auth",
            "django.contrib.contenttypes",
            "django.contrib.sessions",
        ],
        DATABASES={
            "default": {
                "ENGINE": "django.db.backends.sqlite3",
                "NAME": db_path,
            },
        },
        SESSION_ENGINE="django.contrib.sessions.backends.db",
        USE_TZ=True,
    )
    django.setup()

    from django.core.management import call_command

    call_command("migrate", run_syncdb=True, verbosity=0)


_configure_django()


def _make_request(session_key: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-session-token", session_key.encode())],
        }
    )


def _make_cookie_request(session_key: str, cookie_name: str = "sessionid") -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"cookie", f"{cookie_name}={session_key}".encode())] if session_key else [],
        }
    )


@pytest.fixture
def django_user():
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_user(username="jure", password="s3cr3t")  # noqa: S106
    yield user
    user.delete()


@pytest.fixture
def session_key(django_user):
    from importlib import import_module

    from django.conf import settings as django_settings
    from django.contrib.auth import BACKEND_SESSION_KEY, HASH_SESSION_KEY, SESSION_KEY

    engine = import_module(django_settings.SESSION_ENGINE)
    session = engine.SessionStore()
    session[SESSION_KEY] = str(django_user.pk)
    session[BACKEND_SESSION_KEY] = "django.contrib.auth.backends.ModelBackend"
    session[HASH_SESSION_KEY] = django_user.get_session_auth_hash()
    session.save()
    yield session.session_key
    session.delete()


def test_try_handle_returns_none_when_header_missing():
    backend = DjangoSessionAuthBackend()
    assert backend.try_handle(_make_request("")) is None


def test_try_handle_returns_lazy_when_header_present():
    backend = DjangoSessionAuthBackend()
    lazy = backend.try_handle(_make_request("some-key"))
    assert isinstance(lazy, _DjangoSessionUserLazy)


@pytest.mark.asyncio
async def test_resolves_to_real_user_for_valid_session(django_user, session_key):
    backend = DjangoSessionAuthBackend()
    lazy = backend.try_handle(_make_request(session_key))
    user = await lazy
    assert user is not None
    assert user.username == "jure"
    assert user.pk == django_user.pk


@pytest.mark.asyncio
async def test_resolves_to_none_for_unknown_session_key():
    backend = DjangoSessionAuthBackend()
    lazy = backend.try_handle(_make_request("does-not-exist"))
    assert await lazy is None


@pytest.mark.asyncio
async def test_resolves_to_none_after_password_change_invalidates_session(django_user, session_key):
    """django.contrib.auth.get_user() validates the session's stored auth hash - changing the
    password changes that hash, so a previously-valid session stops resolving, exactly like a real
    Django request would behave."""
    from asgiref.sync import sync_to_async

    def change_password():
        django_user.set_password("a-different-password")
        django_user.save()

    await sync_to_async(change_password, thread_sensitive=True)()

    backend = DjangoSessionAuthBackend()
    lazy = backend.try_handle(_make_request(session_key))
    assert await lazy is None


@pytest.mark.asyncio
async def test_serialize_ships_only_the_recipe_never_the_live_user(session_key):
    """The resolved value is a live Django ORM User instance - not JSON-safe - so __serialize__
    must always ship just the recipe (session_key), unlike the default LazyObject shortcut."""
    backend = DjangoSessionAuthBackend()
    lazy = backend.try_handle(_make_request(session_key))
    await lazy  # resolve it

    payload = lazy.__serialize__()
    assert payload == {"recipe": {"session_key": session_key}}
    assert "resolved_value" not in payload

    restored = _DjangoSessionUserLazy.__deserialize__(payload)
    assert restored.is_resolved is False
    user = await restored
    assert user.username == "jure"


def test_custom_header_name():
    backend = DjangoSessionAuthBackend(header_name="x-auth-token")
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"x-auth-token", b"some-key")],
        }
    )
    assert isinstance(backend.try_handle(request), _DjangoSessionUserLazy)
    assert backend.try_handle(_make_request("some-key")) is None


# ---------------------------------------------------------------------------
# DjangoSessionCookieAuthBackend
# ---------------------------------------------------------------------------


def test_cookie_backend_returns_none_when_cookie_missing():
    backend = DjangoSessionCookieAuthBackend()
    assert backend.try_handle(_make_cookie_request("")) is None


def test_cookie_backend_returns_lazy_when_cookie_present():
    backend = DjangoSessionCookieAuthBackend()
    lazy = backend.try_handle(_make_cookie_request("some-key"))
    assert isinstance(lazy, _DjangoSessionUserLazy)


def test_cookie_backend_ignores_the_header():
    backend = DjangoSessionCookieAuthBackend()
    assert backend.try_handle(_make_request("some-key")) is None


def test_cookie_backend_defaults_to_djangos_session_cookie_name():
    """cookie_name=None (the default) means 'whatever django.conf.settings.SESSION_COOKIE_NAME is'
    - Django's own default is "sessionid", already configured for these tests."""
    from django.conf import settings as django_settings

    assert django_settings.SESSION_COOKIE_NAME == "sessionid"
    backend = DjangoSessionCookieAuthBackend()
    assert isinstance(backend.try_handle(_make_cookie_request("some-key", "sessionid")), _DjangoSessionUserLazy)
    assert backend.try_handle(_make_cookie_request("some-key", "other_cookie")) is None


def test_cookie_backend_custom_cookie_name():
    backend = DjangoSessionCookieAuthBackend(cookie_name="my_session")
    assert backend.try_handle(_make_cookie_request("some-key", "sessionid")) is None
    lazy = backend.try_handle(_make_cookie_request("some-key", "my_session"))
    assert isinstance(lazy, _DjangoSessionUserLazy)


@pytest.mark.asyncio
async def test_cookie_backend_resolves_to_real_user_for_valid_session(django_user, session_key):
    """Same underlying resolution/auth-hash validation as the header backend - proven against a
    real Django session, not just a mock."""
    backend = DjangoSessionCookieAuthBackend()
    lazy = backend.try_handle(_make_cookie_request(session_key))
    user = await lazy
    assert user is not None
    assert user.username == "jure"
    assert user.pk == django_user.pk


@pytest.mark.asyncio
async def test_header_and_cookie_backends_compose_via_the_auth_processor_chain(session_key):
    """The documented pattern: register both, first match wins - a request carrying either
    credential shape is recognized."""
    from fastapi_viewsets.conf import settings
    from fastapi_viewsets.context.auth import auth_context_processor

    settings.viewsets_auth_processors = [DjangoSessionCookieAuthBackend(), DjangoSessionAuthBackend()]
    try:
        cookie_result = await auth_context_processor(_make_cookie_request(session_key), None)
        user = await cookie_result["user"]
        assert user.username == "jure"

        header_result = await auth_context_processor(_make_request(session_key), None)
        user = await header_result["user"]
        assert user.username == "jure"
    finally:
        settings.viewsets_auth_processors = []
