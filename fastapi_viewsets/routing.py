"""
The transport a request arrived over, and routes that answer only on the transports they are
published on.

Every viewset endpoint is one route on the application's router, whichever transports publish it,
so a request goes through the application's middleware, exception handlers and router dependencies
whether an HTTP server or the muxws transport delivered it. A route that is not published on the
request's transport does not match, and the router carries on to the next route as if it were not
there.
"""

from collections.abc import Callable
from functools import cache
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.routing import Match
from starlette.types import Scope

from .endpoint_docs import viewset_tags

TRANSPORT_SCOPE_KEY = "fastapi_viewsets.transport"
"""The ASGI scope key a transport writes its name under. A request without it is a REST request."""

REST = "rest"
MUXWS = "muxws"


def request_transport(scope: Scope) -> str:
    """The transport a request arrived over: `scope[TRANSPORT_SCOPE_KEY]`, or `"rest"` without it."""
    return scope.get(TRANSPORT_SCOPE_KEY, REST)


class TransportRoute(APIRoute):
    """An `APIRoute` that matches only requests arriving over one of `transports`."""

    transports: frozenset[str] = frozenset({REST, MUXWS})

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        if scope["type"] == "http" and request_transport(scope) not in self.transports:
            return Match.NONE, {}
        return super().matches(scope)


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
