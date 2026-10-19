"""
muxws transport for viewsets.

The module is `mux_ws`; `muxws` is the WebSocket multiplexing library it builds on
(https://docs.velis.si/muxws).

The viewsets registered over REST are reachable over a single WebSocket as well. A command goes
through the application that serves the WebSocket endpoint, as an HTTP request does: its
middleware, exception handlers, router, dependencies and state. muxws is a transport only - it
turns a stream into an ASGI request and the response back into a reply. Each viewset endpoint is
one route on the application's router, matching only the transports it is published on
(`fastapi_viewsets.routing`).

Server side:

    from fastapi import FastAPI, WebSocket
    from muxws import accept

    from fastapi_viewsets.mux_ws import process_command

    @app.websocket("/ws")
    async def muxws_endpoint(websocket: WebSocket) -> None:
        # Do not call websocket.accept() yourself - muxws performs the upgrade, because only it
        # knows which muxws.v1.<codec> subprotocol to select. A cookie-authenticated WebSocket has
        # no CSRF protection: allowed_origins refuses a foreign Origin with HTTP 403.
        peer = await accept(websocket, allowed_origins=["https://app.example"])
        peer.on_stream(
            lambda payload, stream: process_command(payload, stream, connection=websocket, peer=peer)
        )
        await peer.serve()

Which viewsets get registered is decided at three levels, each able to defer to the next:
`@transports(...)` on an individual endpoint, `register_muxws=` on `route_viewset`, and
`settings.viewsets_register_muxws` globally (default True).
"""

from .protocol import METHOD_KEY, PATH_KEY, QUERY_KEY, STATUS_KEY
from .registry import (
    is_registered,
    register_viewset,
    registered_viewsets,
    reset_registry,
    resolve_register_muxws,
    resolve_register_rest,
    schema_for,
)
from .server import process_command
from .transports import transports

__all__ = [
    "METHOD_KEY",
    "PATH_KEY",
    "QUERY_KEY",
    "STATUS_KEY",
    "is_registered",
    "process_command",
    "register_viewset",
    "registered_viewsets",
    "reset_registry",
    "resolve_register_muxws",
    "resolve_register_rest",
    "schema_for",
    "transports",
]
