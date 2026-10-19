"""
`origin_allowed`, the `Origin` check of a WebSocket endpoint.

A browser sends the cookies of the endpoint's site with a WebSocket handshake whatever page opened
it, and a WebSocket has no CSRF protection of its own: an endpoint whose session is a cookie answers
a handshake from another site's page with that session unless it checks `Origin` before `accept()`.
"""

from collections.abc import Collection
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

_DEFAULT_PORTS = {"http": 80, "https": 443}


def _parse_origin(value: str) -> tuple[str, str, int] | None:
    """`(scheme, host, port)` of a serialized origin, `scheme://host[:port]` with an optional
    trailing `/`, scheme and host lower-cased and the port explicit; None for anything else."""
    text = value.strip()
    if text.endswith("/"):
        text = text[:-1]
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme not in _DEFAULT_PORTS or not parts.hostname:
        return None
    if parts.path or parts.query or parts.fragment or parts.username is not None or parts.password is not None:
        return None
    if "@" in parts.netloc:
        return None
    return scheme, parts.hostname.lower(), port if port is not None else _DEFAULT_PORTS[scheme]


def _parse_host(value: str, default_port: int) -> tuple[str, int] | None:
    """`(host, port)` of a `Host` header value, the port `default_port` when it names none."""
    try:
        parts = urlsplit(f"//{value.strip()}")
        port = parts.port
    except ValueError:
        return None
    if not parts.hostname or parts.path or parts.query or parts.fragment or "@" in parts.netloc:
        return None
    return parts.hostname.lower(), port if port is not None else default_port


def origin_allowed(connection: "HTTPConnection", allowed_origins: Collection[str] | None = None) -> bool:
    """
    Whether the `Origin` of `connection` (a `WebSocket` or a `Request`) may open the endpoint.

    - No `Origin` header (a client that is not a browser): True. More than one: False.
    - `Origin: null`, and an origin that is not `scheme://host[:port]` with the scheme `http` or
      `https` (a path, a query, userinfo, another scheme): False.
    - Origins compare as `(scheme, host, port)`, scheme and host lower-cased, the port the scheme's
      default (80, 443) when the origin names none, a trailing `/` removed.
    - With `allowed_origins`: True when the origin equals one of its entries. An entry that is not
      an origin raises `ValueError`.
    - Without it: True when the origin's `(host, port)` equals that of the request's host, the first
      item of `X-Forwarded-Host` when the request carries one, else `Host`; a host without a port
      takes the default port of the origin's scheme. The scheme is not compared, so a TLS proxy in
      front of the application passes. No host at all: False. A proxy that rewrites `Host` without
      setting `X-Forwarded-Host` needs `allowed_origins`.

    The endpoint calls it before `accept()` and closes a refused handshake, which the server answers
    with HTTP 403:

        if not origin_allowed(websocket):
            await websocket.close(code=1008)
            return
        peer = await accept(websocket)
    """
    allowed = None
    if allowed_origins is not None:
        allowed = set()
        for entry in allowed_origins:
            parsed = _parse_origin(entry)
            if parsed is None:
                raise ValueError(f"{entry!r} in allowed_origins is not an origin: scheme://host[:port]")
            allowed.add(parsed)

    values = connection.headers.getlist("origin")
    if not values:
        return True
    if len(values) > 1:
        return False
    origin = _parse_origin(values[0])
    if origin is None:
        return False
    if allowed is not None:
        return origin in allowed

    scheme, host, port = origin
    forwarded = connection.headers.get("x-forwarded-host")
    request_host = forwarded.split(",")[0] if forwarded else connection.headers.get("host")
    if not request_host or not request_host.strip():
        return False
    parsed_host = _parse_host(request_host, _DEFAULT_PORTS[scheme])
    return parsed_host == (host, port)
