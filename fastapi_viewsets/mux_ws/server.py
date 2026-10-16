"""
The server-side entry point: turn an inbound muxws stream into a viewset call.

muxws allows exactly one `on_stream` handler per peer, so this module never touches the peer
itself. The application keeps ownership of its own handler and calls `process_command` from
inside it; a stream that is not a viewset command is reported as unhandled rather than rejected,
so viewset traffic and the application's own protocol can share one socket.
"""

import asyncio
import logging

from typing import Any, TYPE_CHECKING

from fastapi import FastAPI

from .asgi import call_asgi, payload_to_body
from .protocol import build_response_meta, EnvelopeError, is_command, merge_headers, parse_request

if TYPE_CHECKING:
    from muxws import Stream
    from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)


# Requests whose stream ended before the answer: referenced here until they complete, so that the
# event loop, which keeps only weak references to tasks, does not collect them mid-run.
_detached: set[asyncio.Task] = set()


def _discard_detached(task: asyncio.Task) -> None:
    _detached.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("muxws command failed after its stream ended", exc_info=task.exception())


_CONNECTION_ONLY = frozenset({"connection", "upgrade", "accept-encoding"})


def connection_headers(connection: "WebSocket | None") -> dict[str, str]:
    """
    The handshake's own headers, used as the baseline every command inherits.

    Cookies are already present in the raw `cookie` header, so they need no separate handling -
    the auth backends that read `request.cookies` parse them back out of it themselves.

    The headers that describe the WebSocket upgrade (`connection`, `upgrade`, `sec-websocket-*`)
    are left out, and so is `accept-encoding`: a reply travels as a decoded muxws payload, so a
    compression middleware must not compress it.
    """
    if connection is None:
        return {}
    return {
        key: value
        for key, value in ((key.lower(), value) for key, value in connection.headers.items())
        if key not in _CONNECTION_ONLY and not key.startswith("sec-websocket-")
    }


async def process_command(
    payload: Any,
    stream: "Stream",
    *,
    connection: "WebSocket | None" = None,
    base_headers: dict[str, str] | None = None,
    app: FastAPI | None = None,
) -> bool:
    """
    Serves one viewset command through the application and answers on `stream`.

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

    The command goes through the application that serves the WebSocket endpoint
    (`connection.scope["app"]`), or through `app` where given: its middleware, its exception
    handlers, its router and the dependencies on it, as an HTTP request to the same path would.
    `:path` is the path the application routes, `include_router` prefixes included. Without
    `connection` or `app` a command raises `ValueError`.

    `connection` supplies the handshake headers as a baseline (see `connection_headers`);
    `base_headers` overrides or extends them for callers that resolve their session some other
    way. Headers stated on the stream itself win over both. `connection` also supplies the
    request's client and server address (`request.client`, which `RateLimiter` keys on by
    default), without which every call has the client address `("muxws", 0)`, and the lifespan
    state (`request.state`), copied for each command as an HTTP server copies it for each request.

    The request runs as a task of its own. When the stream ends before the answer (the peer cancels
    it or the socket is lost, and muxws cancels this handler), the request runs to completion and its
    answer is discarded, as an HTTP server lets a request run after its client disconnected. An
    exception it raises then is logged.
    """
    headers = getattr(stream, "headers", None) or {}
    if not is_command(headers):
        return False

    target = app if app is not None else (connection.scope.get("app") if connection is not None else None)
    if target is None:
        raise ValueError("process_command needs connection= (a WebSocket of the application) or app=")

    try:
        method, path, query_string, call_headers = parse_request(headers)
    except EnvelopeError as error:
        # addressed to a viewset, but not in a form any route could be matched against: answered
        # as HTTP answers a request it cannot parse
        await stream.reply({"detail": str(error)}, headers=build_response_meta(400, {}))
        return True
    effective = merge_headers(
        merge_headers(connection_headers(connection), base_headers or {}),
        call_headers,
    )
    body = payload_to_body(payload)
    if body and "content-type" not in effective:
        effective["content-type"] = "application/json"
    effective["content-length"] = str(len(body))

    request = asyncio.ensure_future(
        call_asgi(
            target,
            method=method,
            path=path,
            query_string=query_string,
            headers=effective,
            body=body,
            client=connection.scope.get("client") if connection is not None else None,
            server=connection.scope.get("server") if connection is not None else None,
            state=connection.scope.get("state") if connection is not None else None,
        )
    )
    try:
        response = await asyncio.shield(request)
    except asyncio.CancelledError:
        _detached.add(request)
        request.add_done_callback(_discard_detached)
        raise
    except Exception:
        # The app raised before it started a response. Resetting the stream would turn "the
        # handler crashed" into "the transport failed", which is a different and less useful thing
        # to report - so a 500 is delivered normally and the traceback goes to the log.
        logger.exception("muxws command %s %s failed", method, path)
        await stream.reply(None, headers=build_response_meta(500, {}))
        return True

    # Leading headers rather than trailers: the status is announced before the body, so a caller
    # never has to read a response to find out it was an error. See protocol.py.
    await stream.reply(
        response.json(),
        headers=build_response_meta(response.status, _response_headers(response.headers)),
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
