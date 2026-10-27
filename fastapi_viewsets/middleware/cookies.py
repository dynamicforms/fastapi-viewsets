"""Cookies a `ViewSetResult` sets or deletes, with every option of Starlette's `Response.set_cookie()`
and `Response.delete_cookie()`, and their application to a response."""

from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal, TYPE_CHECKING

from ..context import SerializableObject

if TYPE_CHECKING:
    from starlette.responses import Response


SameSite = Literal["lax", "strict", "none"]


@dataclass
class Cookie(SerializableObject):
    """
    A cookie `ViewSetResult.cookies` sets, with the options of Starlette's `Response.set_cookie()`
    and the same defaults. The HTTP transport applies it with `response.set_cookie(name, ...)`.

    `max_age` is in seconds. `expires` is a `datetime` (sent as an HTTP date), a number of seconds
    from now, or a string sent as it is. `partitioned=True` is passed on to Starlette, which raises
    `ValueError` where the running Python does not support the attribute; False is not passed on.
    """

    value: str = ""
    max_age: int | None = None
    expires: datetime | str | int | None = None
    path: str | None = "/"
    domain: str | None = None
    secure: bool = False
    httponly: bool = False
    samesite: SameSite | None = "lax"
    partitioned: bool = False

    def __serialize__(self) -> Any:
        data = asdict(self)
        if isinstance(self.expires, datetime):
            data["expires"] = {"datetime": self.expires.isoformat()}
        return data

    @classmethod
    def __deserialize__(cls, data: Any) -> "Cookie":
        data = dict(data)
        if isinstance(data["expires"], dict):
            data["expires"] = datetime.fromisoformat(data["expires"]["datetime"])
        return cls(**data)


@dataclass
class DeleteCookie(SerializableObject):
    """
    Deletion of a cookie in `ViewSetResult.cookies`, with the options of Starlette's
    `Response.delete_cookie()` and the same defaults. The HTTP transport applies it with
    `response.delete_cookie(name, ...)`: a `Set-Cookie` with an empty value, `Max-Age=0` and an
    expiry in the past. `path` and `domain` must be those the cookie was set with, or the browser
    keeps the cookie.
    """

    path: str = "/"
    domain: str | None = None
    secure: bool = False
    httponly: bool = False
    samesite: SameSite | None = "lax"

    def __serialize__(self) -> Any:
        return asdict(self)

    @classmethod
    def __deserialize__(cls, data: Any) -> "DeleteCookie":
        return cls(**data)


def apply_cookies(response: "Response", cookies: dict[str, Any]) -> None:
    """Applies `ViewSetResult.cookies` to `response`: `Response.set_cookie()` for a `Cookie`,
    `Response.delete_cookie()` for a `DeleteCookie`, `Response.set_cookie(name, str(value))` for any
    other value."""
    for key, value in cookies.items():
        if isinstance(value, Cookie):
            options = asdict(value)
            if not options["partitioned"]:
                del options["partitioned"]
            response.set_cookie(key, **options)
        elif isinstance(value, DeleteCookie):
            response.delete_cookie(key, **asdict(value))
        else:
            response.set_cookie(key, str(value))
