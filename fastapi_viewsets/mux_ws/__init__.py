"""
muxws transport for viewsets (https://docs.velis.si/muxws): every viewset endpoint is reachable over one
WebSocket as well as REST.

A command is run through the application serving the WebSocket endpoint, as an HTTP request is (middleware,
exception handlers, dependencies). Call `process_command` from the peer's `on_stream`. Registration is decided by
`@transports(...)`, then `route_viewset(register_muxws=)`, then `settings.viewsets_register_muxws`.
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
