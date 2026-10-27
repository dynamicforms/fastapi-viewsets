"""
Runs one muxws command through the FastAPI app as an in-process ASGI request, so binding, validation,
dependencies and serialisation are the same as over HTTP. The scope marks the transport as muxws.
"""

import json
import logging

from typing import Any

from starlette.types import ASGIApp

from ..routing import MUXWS, PEER_SCOPE_KEY, TRANSPORT_SCOPE_KEY

logger = logging.getLogger(__name__)


class AsgiResponse:
    """What came back out of the app: status, headers and the raw body bytes."""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        """The decoded JSON body, None when empty, or the body as a string when it is not JSON."""
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.body.decode("utf-8", errors="replace")


async def call_asgi(
    app: ASGIApp,
    *,
    method: str,
    path: str,
    query_string: bytes = b"",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
    client: tuple[str, int] | None = None,
    server: tuple[str, int] | None = None,
    state: dict[str, Any] | None = None,
    peer: Any = None,
) -> AsgiResponse:
    """
    One ASGI round trip. `client`/`server` default to `("muxws", 0)`; `state` (lifespan state) is
    copied shallowly; `peer` goes to `scope[PEER_SCOPE_KEY]`. An exception after the response started
    (Starlette's 500) is logged and the response returned; one before it propagates.
    """
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query_string,
        "root_path": "",
        "headers": [(key.lower().encode(), str(value).encode()) for key, value in (headers or {}).items()],
        "client": client or ("muxws", 0),
        "server": server or ("muxws", 0),
        "state": dict(state or {}),
        TRANSPORT_SCOPE_KEY: MUXWS,
        PEER_SCOPE_KEY: peer,
    }

    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if request_sent:
            # Reading past the body must see a disconnect, not block forever.
            return {"type": "http.disconnect"}
        request_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    status = 500
    started = False
    response_headers: dict[str, str] = {}
    chunks: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        nonlocal status, started
        if message["type"] == "http.response.start":
            started = True
            status = message["status"]
            for raw_key, raw_value in message.get("headers", []):
                response_headers[raw_key.decode("latin-1").lower()] = raw_value.decode("latin-1")
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    try:
        await app(scope, receive, send)
    except Exception:
        if not started:
            raise
        logger.exception("muxws command %s %s failed", method, path)
    return AsgiResponse(status, response_headers, b"".join(chunks))


def payload_to_body(payload: Any) -> bytes:
    """A decoded muxws payload as JSON body bytes; `bytes` pass through, absent or None is empty."""
    from muxws.frames import ABSENT

    if payload is ABSENT or payload is None:
        return b""
    if isinstance(payload, bytes):
        return payload
    return json.dumps(payload).encode()
