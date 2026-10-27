"""
The call data a `celery_viewset` task carries in reserved kwargs beside the action's arguments: the
call's context, request, parent execution and untyped arguments.
"""

from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder

CONTEXT_KWARG = "_context"
"""The call's context, as `serialize_context` encodes it."""
REQUEST_KWARG = "_request"
"""The call's request, as `request_to_jsonable` encodes it; None without one."""
UNTYPED_KWARG = "_untyped"
"""The names of the arguments that crossed untyped (see `transport`); absent when there are none."""
PARENT_KWARG = "_parent"
"""The `ExecutionRef` the call was dispatched in (`ExecutionRef.to_jsonable`); None for a request's call."""


def _address(value: Any) -> list | None:
    return list(value) if value is not None else None


def request_to_jsonable(request: Request | None) -> dict[str, Any] | None:
    """The ASGI scope of `request` without its body, `app` and `state`, JSON-safe."""
    if request is None:
        return None
    scope = request.scope
    return {
        "type": scope.get("type", "http"),
        "http_version": scope.get("http_version", "1.1"),
        "method": scope.get("method", "GET"),
        "scheme": scope.get("scheme", "http"),
        "path": scope.get("path", ""),
        "root_path": scope.get("root_path", ""),
        "query_string": scope.get("query_string", b"").decode("latin-1"),
        "headers": [[name.decode("latin-1"), value.decode("latin-1")] for name, value in scope.get("headers", [])],
        "path_params": jsonable_encoder(scope.get("path_params", {})),
        "client": _address(scope.get("client")),
        "server": _address(scope.get("server")),
    }


def request_from_jsonable(data: dict[str, Any] | None) -> Request | None:
    """A `Request` over the scope `request_to_jsonable` encoded. Reading its body raises."""
    if data is None:
        return None
    scope = {
        **data,
        "query_string": data["query_string"].encode("latin-1"),
        "headers": [(name.encode("latin-1"), value.encode("latin-1")) for name, value in data["headers"]],
        "client": tuple(data["client"]) if data["client"] is not None else None,
        "server": tuple(data["server"]) if data["server"] is not None else None,
    }
    return Request(scope)
