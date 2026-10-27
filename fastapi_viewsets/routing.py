"""
Request transports (REST, muxws) and routes that match only on the transports they are published on.
Both transports go through the same application router, middleware and exception handlers.
"""

from collections.abc import Callable
from functools import cache
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.exceptions import HTTPException
from starlette.routing import Match
from starlette.types import Message, Receive, Scope, Send

from .endpoint_docs import viewset_tags
from .error_responses import answers_unhandled_itself, error_response, uniform_exception_handlers

TRANSPORT_SCOPE_KEY = "fastapi_viewsets.transport"
"""The ASGI scope key a transport writes its name under. A request without it is a REST request."""

PEER_SCOPE_KEY = "fastapi_viewsets.peer"
"""ASGI scope key of the muxws peer a command arrived on (None if not passed); absent on REST."""

GATE_RUN_SCOPE_KEY = "fastapi_viewsets.events.gate"
"""ASGI scope key set on an events gate run's request, holding the gate action's name."""

REST = "rest"
MUXWS = "muxws"


def request_transport(scope: Scope) -> str:
    """The transport a request arrived over: `scope[TRANSPORT_SCOPE_KEY]`, or `"rest"` without it."""
    return scope.get(TRANSPORT_SCOPE_KEY, REST)


class TransportRoute(APIRoute):
    """
    An `APIRoute` matching only requests over one of `transports`, answering its errors with an
    `ApiErrorBody` without registered handlers. An unhandled exception becomes 500 `unhandled_error`
    and is re-raised for logging; the application's own handlers still take precedence.
    """

    transports: frozenset[str] = frozenset({REST, MUXWS})

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] == "http" and request_transport(scope) not in self.transports:
            return Match.NONE, {}
        return super().matches(scope)

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        uniform_exception_handlers(scope)
        started = False

        async def sending(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await super().handle(scope, receive, sending)
        except Exception as error:
            # an HTTPException that reaches here is Starlette's 405 of the route, not the endpoint's
            if started or isinstance(error, HTTPException) or answers_unhandled_itself(scope.get("app")):
                raise
            await error_response(error)(scope, receive, send)
            raise


@cache
def transport_route_class(base: type[APIRoute], transports: frozenset[str]) -> type[APIRoute]:
    """
    `base` restricted to `transports`, one cached class per combination (`add_api_route` takes a class,
    so the transports travel as a class attribute).
    """
    name = f"{base.__name__}On{''.join(sorted(transport.capitalize() for transport in transports))}"
    return type(name, (TransportRoute, base), {"transports": transports})


def openapi_endpoint(route_kwargs: list[dict[str, Any]]) -> Callable[[], dict[str, Any]]:
    """
    A `/schema` endpoint with the OpenAPI document of `route_kwargs` (`add_api_route` form), built on
    first call so the list may be filled later.
    """
    app: FastAPI | None = None

    def schema() -> dict[str, Any]:
        nonlocal app
        if app is None:
            app = FastAPI(openapi_tags=viewset_tags())
            for kwargs in route_kwargs:
                app.router.add_api_route(**kwargs)
        return app.openapi()

    return schema
