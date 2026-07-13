import json

from pathlib import Path
from typing import Any, TYPE_CHECKING

from .. import LazyObject
from . import AuthBackend

if TYPE_CHECKING:
    from fastapi import Request


class _StaticUserLazy(LazyObject):
    """resolve() is sync and needs no I/O - a real showcase of LazyObject's sync mode: `.value`
    is available the moment anything awaits it, no coroutine/Future ever gets scheduled."""

    def __init__(self, user_data: dict[str, Any]):
        super().__init__()
        self._user_data = user_data

    def resolve(self) -> Any:
        return self._user_data

    def serialize_recipe(self) -> Any:
        # Already JSON-safe, and cheap enough that "recipe" and "resolved value" are the same
        # thing - no separate lookup needed to reconstruct it on the other side of a Celery hop.
        return self._user_data

    @classmethod
    def deserialize_recipe(cls, data: Any) -> "_StaticUserLazy":
        return cls(data)


class StaticUserAuthBackend(AuthBackend):
    """
    Ad-hoc auth backend for prototyping/tests: a fixed mapping of session token -> user data (e.g.
    loaded from a small JSON file), no real session store involved.

        backend = StaticUserAuthBackend({"tok-jure": {"id": 1, "username": "jure"}})
        # or: StaticUserAuthBackend.from_json_file("users.json")
        settings.viewsets_auth_processors = [backend]

    Requests carry the token in the `X-Session-Token` **header** (configurable via `header_name`).
    For a cookie instead, see `StaticUserCookieAuthBackend` below - register both to accept either:

        settings.viewsets_auth_processors = [StaticUserCookieAuthBackend(users), StaticUserAuthBackend(users)]
    """

    def __init__(self, users_by_token: dict[str, dict[str, Any]], header_name: str = "x-session-token"):
        self.users_by_token = users_by_token
        self.header_name = header_name

    @classmethod
    def from_json_file(cls, path: str | Path, header_name: str = "x-session-token") -> "StaticUserAuthBackend":
        """`path` should contain a JSON object mapping token -> user data, e.g.
        `{"tok-jure": {"id": 1, "username": "jure"}}`."""
        return cls(json.loads(Path(path).read_text()), header_name)

    def try_handle(self, request: "Request") -> LazyObject | None:
        token = request.headers.get(self.header_name)
        if token is None or token not in self.users_by_token:
            return None  # not a token we recognize - let the next backend try it
        return _StaticUserLazy(self.users_by_token[token])


class StaticUserCookieAuthBackend(AuthBackend):
    """
    Same fixed token -> user data mapping as `StaticUserAuthBackend`, but reads the token from a
    **cookie** instead of a header - for exercising/prototyping a real browser-cookie flow without
    a real session store.
    """

    def __init__(self, users_by_token: dict[str, dict[str, Any]], cookie_name: str = "sessionid"):
        self.users_by_token = users_by_token
        self.cookie_name = cookie_name

    @classmethod
    def from_json_file(cls, path: str | Path, cookie_name: str = "sessionid") -> "StaticUserCookieAuthBackend":
        """`path` should contain a JSON object mapping token -> user data, e.g.
        `{"tok-jure": {"id": 1, "username": "jure"}}`."""
        return cls(json.loads(Path(path).read_text()), cookie_name)

    def try_handle(self, request: "Request") -> LazyObject | None:
        token = request.cookies.get(self.cookie_name)
        if token is None or token not in self.users_by_token:
            return None  # not a token we recognize - let the next backend try it
        return _StaticUserLazy(self.users_by_token[token])
