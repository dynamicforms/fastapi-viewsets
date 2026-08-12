"""
muxws transport for viewsets.

The module is spelled `mux_ws`, with the underscore, and the underscore is deliberate: `muxws` is
the WebSocket multiplexing library this builds on (https://docs.velis.si/muxws), and having an
identically named module inside this package would make every import in this file ambiguous to
the reader even where it is unambiguous to Python. Nothing here is a typo.

What this gives you is the same viewsets you already registered over REST, reachable over a
single WebSocket instead - dispatched through the very same FastAPI application object, so
validation, dependencies, command middleware, context processors and response models behave
identically on both transports.

Server side:

    from fastapi import FastAPI, WebSocket
    from muxws import accept

    from fastapi_viewsets.mux_ws import process_command

    @app.websocket("/ws")
    async def muxws_endpoint(websocket: WebSocket) -> None:
        # Do not call websocket.accept() yourself - muxws performs the upgrade, because only it
        # knows which muxws.v1.<codec> subprotocol to select.
        peer = await accept(websocket)
        peer.on_stream(lambda payload, stream: process_command(payload, stream, connection=websocket))
        await peer.serve()

Which viewsets get registered is decided at three levels, each able to defer to the next:
`@transports(...)` on an individual endpoint, `register_muxws=` on `route_viewset`, and
`settings.viewsets_register_muxws` globally (default True).
"""

from .protocol import METHOD_KEY, PATH_KEY, QUERY_KEY, STATUS_KEY
from .registry import (
    get_dispatch_app,
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
    "get_dispatch_app",
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
