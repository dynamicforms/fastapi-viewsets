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
    The Django user of a session key, resolved through `sync_to_async`. Serializes only the recipe
    (`session_key`), as a Django User instance is not JSON-safe; a Celery worker re-resolves it.
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
    Resolves the caller from the Django session named by the `X-Session-Token` header, as Django's
    `get_user()` does (auth hash validated, `SESSION_ENGINE` honoured), for non-browser clients. Use
    `DjangoSessionCookieAuthBackend` for the browser's session cookie; both can be registered. Needs
    the `django` extra and a Django the host application has already set up.
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
    `DjangoSessionAuthBackend`, reading the key from Django's session cookie, for browser clients.
    `cookie_name` None means `django.conf.settings.SESSION_COOKIE_NAME`.
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
