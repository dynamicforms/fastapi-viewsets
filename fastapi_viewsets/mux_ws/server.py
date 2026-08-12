"""
The server-side entry point: turn an inbound muxws stream into a viewset call.

muxws allows exactly one `on_stream` handler per peer, so this module never touches the peer
itself. The application keeps ownership of its own handler and calls `process_command` from
inside it; a stream that is not a viewset command is reported as unhandled rather than rejected,
so viewset traffic and the application's own protocol can share one socket.
"""

import logging

from typing import Any, TYPE_CHECKING

from fastapi import FastAPI

from .asgi import call_asgi, payload_to_body
from .protocol import build_response_meta, is_command, merge_headers, parse_request
from .registry import get_dispatch_app

if TYPE_CHECKING:
    from muxws import Stream
    from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)


def connection_headers(connection: "WebSocket | None") -> dict[str, str]:
    """
    The handshake's own headers, used as the baseline every command inherits.

    Cookies are already present in the raw `cookie` header, so they need no separate handling -
    the auth backends that read `request.cookies` parse them back out of it themselves.
    """
    if connection is None:
        return {}
    return {key.lower(): value for key, value in connection.headers.items()}


async def process_command(
    payload: Any,
    stream: "Stream",
    *,
    connection: "WebSocket | None" = None,
    base_headers: dict[str, str] | None = None,
    app: FastAPI | None = None,
) -> bool:
    """
    Serves one viewset command and answers on `stream`.

    Returns True when the stream was a viewset command and has been answered, False when it was
    not addressed to a viewset at all - in which case nothing has been sent and the caller is free
    to dispatch it however it likes:

        @app.websocket("/ws")
        async def endpoint(websocket: WebSocket) -> None:
            peer = await accept(websocket)

            async def on_stream(payload, stream):
                if await process_command(payload, stream, connection=websocket):
                    return
                await my_own_dispatch(payload, stream)

            peer.on_stream(on_stream)
            await peer.serve()

    `connection` supplies the handshake headers as a baseline; `base_headers` overrides or extends
    them for callers that resolve their session some other way. Headers stated on the stream
    itself win over both.
    """
    headers = getattr(stream, "headers", None) or {}
    if not is_command(headers):
        return False

    method, path, query_string, call_headers = parse_request(headers)
    effective = merge_headers(
        merge_headers(connection_headers(connection), base_headers or {}),
        call_headers,
    )
    body = payload_to_body(payload)
    if body and "content-type" not in effective:
        effective["content-type"] = "application/json"
    effective["content-length"] = str(len(body))

    target = app or get_dispatch_app()
    try:
        response = await call_asgi(
            target, method=method, path=path, query_string=query_string, headers=effective, body=body
        )
    except Exception:
        # The app has already produced a 500 for this by the time it re-raises, and an HTTP client
        # would simply have received it. Resetting the stream instead would turn "the handler
        # crashed" into "the transport failed", which is a different and less useful thing to
        # report - so the status is delivered normally and the traceback goes to the log, where it
        # would have gone anyway.
        logger.exception("muxws command %s %s failed", method, path)
        await stream.reply(None, trailers=build_response_meta(500, {}))
        return True

    await stream.reply(
        response.json(),
        trailers=build_response_meta(response.status, _response_headers(response.headers)),
    )
    return True


_HOP_BY_HOP = frozenset({"content-length", "transfer-encoding", "connection", "keep-alive"})


def _response_headers(headers: dict[str, str]) -> dict[str, str]:
    """
    Framing headers describe an HTTP message that no longer exists once the body is a decoded
    muxws payload, so they are dropped. `set-cookie` and the rest are kept: command middleware
    sets them deliberately and the client still wants to see them.
    """
    return {key: value for key, value in headers.items() if key not in _HOP_BY_HOP}
