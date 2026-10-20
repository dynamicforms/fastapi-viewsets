"""
The transport a request arrived over, and routes that answer only on the transports they are
published on.

Every viewset endpoint is one route on the application's router, whichever transports publish it,
so a request goes through the application's middleware, exception handlers and router dependencies
whether an HTTP server or the muxws transport delivered it. A route that is not published on the
request's transport does not match, and the router carries on to the next route as if it were not
there. A route answers every error its request raises with the body of
`fastapi_viewsets.error_responses`.
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
"""The ASGI scope key `mux_ws.process_command` writes its `peer` argument under: the muxws peer of the
socket the command arrived on, or None when the caller passed none. Absent from a REST request."""

GATE_RUN_SCOPE_KEY = "fastapi_viewsets.events.gate"
"""The ASGI scope key present on the request of an events gate run; its value is the gate action's
name. Re-exported as `fastapi_viewsets.events.GATE_RUN_SCOPE_KEY`."""

REST = "rest"
MUXWS = "muxws"


def request_transport(scope: Scope) -> str:
    """The transport a request arrived over: `scope[TRANSPORT_SCOPE_KEY]`, or `"rest"` without it."""
    return scope.get(TRANSPORT_SCOPE_KEY, REST)


class TransportRoute(APIRoute):
    """
    An `APIRoute` that matches only requests arriving over one of `transports`, and answers every
    error its endpoint, its dependencies or its request validation raise with an `ApiErrorBody`
    (`fastapi_viewsets.error_responses.error_body`), without a handler registered by the application:

    - the handlers of `HTTPException` and `RequestValidationError` replace FastAPI's default
      handlers;
    - an exception without a handler is answered with 500 `fastapi-viewsets.unhandled_error` and
      re-raised, so the server logs it as it logs an exception after Starlette's own 500. A handler
      the application registered for `Exception` or 500 answers it instead.

    A handler the application registered for a class or a status answers as the application wrote
    it. The exceptions run through the request's dependencies with `yield` before they are answered.
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
    A route class that is `base` (the router's own `route_class`) restricted to `transports`.

    One class per combination, created once: `add_api_route` takes a route class, not arguments
    for it, so the transports travel as a class attribute.
    """
    name = f"{base.__name__}On{''.join(sorted(transport.capitalize() for transport in transports))}"
    return type(name, (TransportRoute, base), {"transports": transports})


def openapi_endpoint(route_kwargs: list[dict[str, Any]]) -> Callable[[], dict[str, Any]]:
    """
    A `/schema` endpoint answering with the OpenAPI document of the routes `route_kwargs` lists, in
    `add_api_route` form. The document is built on the first call, so the list may still be filled
    after the endpoint is created.
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
