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
    real Django request) - except the session key arrives via an `X-Session-Token` header instead
    of Django's own session cookie, for non-browser/cross-origin clients.

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
