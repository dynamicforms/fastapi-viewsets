"""
The server-side entry point: turn an inbound muxws stream into a viewset call. The application keeps
its own `on_stream` handler and calls `process_command` from it; a stream that is not a viewset
command is reported as unhandled, so viewset traffic and the application's protocol share one socket.
"""

import asyncio
import logging

from typing import Any, TYPE_CHECKING

from fastapi import FastAPI

from ..error_responses import error_body
from ..exceptions import CommandInvalidError, DfViewSetError, UnhandledError
from .asgi import call_asgi, payload_to_body
from .protocol import build_response_meta, EnvelopeError, is_command, merge_headers, parse_request

if TYPE_CHECKING:
    from muxws import Stream
    from starlette.websockets import WebSocket

logger = logging.getLogger(__name__)


# Requests whose stream ended before the answer, held until they complete: the loop keeps tasks weakly.
_detached: set[asyncio.Task] = set()


def _discard_detached(task: asyncio.Task) -> None:
    _detached.discard(task)
    if not task.cancelled() and task.exception() is not None:
        logger.error("muxws command failed after its stream ended", exc_info=task.exception())


_CONNECTION_ONLY = frozenset({"connection", "upgrade", "accept-encoding"})


def connection_headers(connection: "WebSocket | None") -> dict[str, str]:
    """
    The handshake's headers, the baseline every command inherits (cookies included). The upgrade
    headers are left out, and so is `accept-encoding`, as a reply travels as a decoded muxws payload.
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
    peer: Any = None,
) -> bool:
    """
    Serves one viewset command through the application (`app`, else `connection.scope["app"]`, else
    `ValueError`) as an HTTP request to `:path` would be served, and answers on `stream`. Returns False,
    sending nothing, for a stream that is not a viewset command.

    Headers: the handshake's (`connection`), then `base_headers`, then the stream's own. `connection`
    also supplies the client address (else `("muxws", 0)`) and the lifespan state; `peer` reaches the
    request as `scope["fastapi_viewsets.peer"]`, which event subscriptions need. A request whose stream
    ends early runs to completion and its answer is discarded. Unparsable headers answer 400
    `command_invalid`; an exception before the response answers 500 `unhandled_error` and is logged.
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
        # addressed to a viewset but unroutable: answered as HTTP answers a request it cannot parse
        await _reply_error(stream, CommandInvalidError(error.reason))
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
            peer=peer,
        )
    )
    try:
        response = await asyncio.shield(request)
    except asyncio.CancelledError:
        _detached.add(request)
        request.add_done_callback(_discard_detached)
        raise
    except Exception:
        # Answered with a 500 rather than a stream reset, which the client would read as a transport failure.
        logger.exception("muxws command %s %s failed", method, path)
        await _reply_error(stream, UnhandledError())
        return True

    # The status travels in leading headers, before the body; see protocol.py.
    await stream.reply(
        response.json(),
        headers=build_response_meta(response.status, _response_headers(response.headers)),
    )
    return True


async def _reply_error(stream: "Stream", error: DfViewSetError) -> None:
    """Answers `stream` with `error`'s status and `ApiErrorBody`, as a route answers it."""
    await stream.reply(error_body(error), headers=build_response_meta(error.status_code, {}))


_HOP_BY_HOP = frozenset({"content-length", "transfer-encoding", "connection", "keep-alive"})


def _response_headers(headers: dict[str, str]) -> dict[str, str]:
    """The response headers without the HTTP framing ones, which do not apply to a decoded muxws payload."""
    return {key: value for key, value in headers.items() if key not in _HOP_BY_HOP}
