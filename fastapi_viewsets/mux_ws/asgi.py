"""
Runs one request through a FastAPI app without a socket underneath it.

The muxws transport deliberately does not re-implement parameter binding, validation, dependency
resolution or response-model serialisation - it hands the request to the very same FastAPI
application object the REST transport would have used, so the two transports cannot drift apart.
What that costs is one synthetic ASGI round trip per call, which is still nothing next to an
actual HTTP request: no connection setup, no header parsing, no socket at all.
"""

import json

from typing import Any

from fastapi import FastAPI


class AsgiResponse:
    """What came back out of the app: status, headers and the raw body bytes."""

    __slots__ = ("status", "headers", "body")

    def __init__(self, status: int, headers: dict[str, str], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self) -> Any:
        """
        The decoded body, or None when there is none (204, or a handler that returned nothing).

        A body that is not valid JSON is returned as a string rather than raising: by the time we
        get here the app has already decided this is a successful response, and turning a
        content-type surprise into a transport-level failure would be the wrong trade.
        """
        if not self.body:
            return None
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return self.body.decode("utf-8", errors="replace")


async def call_asgi(
    app: FastAPI,
    *,
    method: str,
    path: str,
    query_string: bytes = b"",
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> AsgiResponse:
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
        "client": ("muxws", 0),
        "server": ("muxws", 0),
        "state": {},
    }

    request_sent = False

    async def receive() -> dict[str, Any]:
        nonlocal request_sent
        if request_sent:
            # An app that reads past the end of the body must see a disconnect rather than block
            # forever - there is no socket here to eventually drop.
            return {"type": "http.disconnect"}
        request_sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    status = 500
    response_headers: dict[str, str] = {}
    chunks: list[bytes] = []

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]
            for raw_key, raw_value in message.get("headers", []):
                response_headers[raw_key.decode("latin-1").lower()] = raw_value.decode("latin-1")
        elif message["type"] == "http.response.body":
            chunks.append(message.get("body", b""))

    await app(scope, receive, send)
    return AsgiResponse(status, response_headers, b"".join(chunks))


def payload_to_body(payload: Any) -> bytes:
    """
    Re-encodes an already-decoded muxws payload as the JSON bytes FastAPI expects to parse.

    This decode-then-re-encode round trip is the one real inefficiency of dispatching through
    ASGI, and it is deliberate: skipping it would mean binding and validating parameters
    ourselves, which is exactly the duplication this transport exists to avoid. See GAPS.md.
    """
    from muxws.frames import ABSENT

    if payload is ABSENT or payload is None:
        return b""
    if isinstance(payload, bytes):
        return payload
    return json.dumps(payload).encode()
