from typing import Any, TYPE_CHECKING

from .. import LazyObject
from . import AuthBackend

if TYPE_CHECKING:
    from fastapi import Request


class _SessionOnlyRequest:
    """The bare minimum `django.contrib.auth.get_user()` needs from a request: a `.session`."""

    session: Any


class _DjangoSessionUserLazy(LazyObject):
    """
    resolve() is async (real DB/cache I/O via Django's session engine) - `_fetch_user` runs the
    actual Django ORM/session work through `sync_to_async`, so the event loop isn't blocked.

    Deliberately does *not* use the inherited recipe+resolved_value-shortcut __serialize__: the
    resolved value here is a live Django ORM User instance, not JSON-safe, so it can never travel
    through Celery/Redis. __serialize__ is overridden to always ship just the recipe
    (`session_key`) - a Celery worker re-resolves from it (one extra query, but correct) rather
    than receiving something un-picklable.
    """

    def __init__(self, session_key: str):
        super().__init__()
        self.session_key = session_key

    def resolve(self) -> Any:
        return self._fetch_user()

    async def _fetch_user(self) -> Any:
        from asgiref.sync import sync_to_async

        return await sync_to_async(self._fetch_user_sync, thread_sensitive=True)()

    def _fetch_user_sync(self) -> Any:
        from importlib import import_module

        from django.conf import settings as django_settings
        from django.contrib.auth import get_user

        engine = import_module(django_settings.SESSION_ENGINE)
        session = engine.SessionStore(session_key=self.session_key)
        if not session.exists(self.session_key):
            return None  # unknown/expired session

        fake_request = _SessionOnlyRequest()
        fake_request.session = session
        user = get_user(fake_request)
        return None if user.is_anonymous else user

    def __serialize__(self) -> Any:
        return {"recipe": self.serialize_recipe()}

    def serialize_recipe(self) -> Any:
        return {"session_key": self.session_key}

    @classmethod
    def deserialize_recipe(cls, data: Any) -> "_DjangoSessionUserLazy":
        return cls(data["session_key"])


class DjangoSessionAuthBackend(AuthBackend):
    """
    Resolves the caller from a real Django session, the same way Django's own
    `AuthenticationMiddleware` does (`django.contrib.auth.get_user`, which also validates the
    session's auth hash - a changed password invalidates the session exactly as it would for a
    real Django request) - via an `X-Session-Token` **header**, for non-browser/cross-origin
    clients (a mobile app, a service-to-service call, allauth's headless "app" client mode). For a
    real browser session cookie (allauth's headless "browser" client mode, or plain Django), see
    `DjangoSessionCookieAuthBackend` below instead - register both (in whichever order fits your
    traffic) to accept either:

        settings.viewsets_auth_processors = [DjangoSessionCookieAuthBackend(), DjangoSessionAuthBackend()]

    Uses whichever backend `django.conf.settings.SESSION_ENGINE` names (DB, cache, Redis, ...),
    same as Django itself.

    Requires the `django` extra (`pip install "dynamicforms-fastapi-viewsets[django]"`) and a
    Django app already configured/`django.setup()`'d by the host application - this backend
    doesn't configure Django itself, it only uses whatever's already set up.
    """

    def __init__(self, header_name: str = "x-session-token"):
        self.header_name = header_name

    def try_handle(self, request: "Request") -> LazyObject | None:
        session_key = request.headers.get(self.header_name)
        if not session_key:
            return None
        return _DjangoSessionUserLazy(session_key)


class DjangoSessionCookieAuthBackend(AuthBackend):
    """
    Same resolution as `DjangoSessionAuthBackend` (same `_DjangoSessionUserLazy`, same auth-hash
    validation, same Celery-safe recipe-only serialization) - but reads Django's own session
    **cookie** instead of a header, for real browser clients (allauth's headless "browser" client
    mode, or plain Django/allauth without headless at all). This is the one that actually works
    with a standard HttpOnly `sessionid` cookie - unlike a header, a cookie is attached to the
    request automatically by the browser, and (being HttpOnly) can't be read by JavaScript to be
    copied into a header even if you wanted to.

    `cookie_name` defaults to `None`, meaning "whatever `django.conf.settings.SESSION_COOKIE_NAME`
    is" (Django's own default is `"sessionid"`) - pass an explicit name to override.
    """

    def __init__(self, cookie_name: str | None = None):
        self.cookie_name = cookie_name

    def _resolve_cookie_name(self) -> str:
        if self.cookie_name is not None:
            return self.cookie_name
        from django.conf import settings as django_settings

        return django_settings.SESSION_COOKIE_NAME

    def try_handle(self, request: "Request") -> LazyObject | None:
        session_key = request.cookies.get(self._resolve_cookie_name())
        if not session_key:
            return None
        return _DjangoSessionUserLazy(session_key)
