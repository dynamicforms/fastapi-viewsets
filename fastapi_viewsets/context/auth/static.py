import json

from pathlib import Path
from typing import Any, TYPE_CHECKING

from .. import LazyObject
from . import AuthBackend

if TYPE_CHECKING:
    from fastapi import Request


class _StaticUserLazy(LazyObject):
    """A LazyObject that resolves synchronously, without I/O."""

    def __init__(self, user_data: dict[str, Any]):
        super().__init__()
        self._user_data = user_data

    def resolve(self) -> Any:
        return self._user_data

    def serialize_recipe(self) -> Any:
        # The user data is JSON-safe, so it is its own recipe.
        return self._user_data

    @classmethod
    def deserialize_recipe(cls, data: Any) -> "_StaticUserLazy":
        return cls(data)


class StaticUserAuthBackend(AuthBackend):
    """
    Prototyping/test backend: a fixed mapping of token -> user data, the token read from the
    `header_name` header (`X-Session-Token`). `StaticUserCookieAuthBackend` reads a cookie instead.
    """

    def __init__(self, users_by_token: dict[str, dict[str, Any]], header_name: str = "x-session-token"):
        self.users_by_token = users_by_token
        self.header_name = header_name

    @classmethod
    def from_json_file(cls, path: str | Path, header_name: str = "x-session-token") -> "StaticUserAuthBackend":
        """Loads the token -> user data mapping from a JSON object file."""
        return cls(json.loads(Path(path).read_text()), header_name)

    def try_handle(self, request: "Request") -> LazyObject | None:
        token = request.headers.get(self.header_name)
        if token is None or token not in self.users_by_token:
            return None
        return _StaticUserLazy(self.users_by_token[token])


class StaticUserCookieAuthBackend(AuthBackend):
    """`StaticUserAuthBackend` reading the token from the `cookie_name` cookie."""

    def __init__(self, users_by_token: dict[str, dict[str, Any]], cookie_name: str = "sessionid"):
        self.users_by_token = users_by_token
        self.cookie_name = cookie_name

    @classmethod
    def from_json_file(cls, path: str | Path, cookie_name: str = "sessionid") -> "StaticUserCookieAuthBackend":
        """Loads the token -> user data mapping from a JSON object file."""
        return cls(json.loads(Path(path).read_text()), cookie_name)

    def try_handle(self, request: "Request") -> LazyObject | None:
        token = request.cookies.get(self.cookie_name)
        if token is None or token not in self.users_by_token:
            return None
        return _StaticUserLazy(self.users_by_token[token])
