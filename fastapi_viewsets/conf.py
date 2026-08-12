import json

from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request
    from fastapi.security.base import SecurityBase

    from .context.auth import AuthBackend
    from .middleware import CommandMiddleware

ContextProcessor = Callable[["Request", Any], Awaitable[dict[str, Any]]]


class Settings:
    """
    Global, mutable settings for fastapi_viewsets - configured once by the application, e.g.:

        from fastapi_viewsets.conf import settings

        async def auth_processor(request, viewset) -> dict:
            return {"user": await authenticate(request)}

        settings.viewsets_context_processors = [auth_processor]
    """

    def __init__(self):
        self.viewsets_context_processors: list[ContextProcessor] = []
        self.viewsets_context_json_encoder: type[json.JSONEncoder] | None = None
        self.viewsets_context_json_decoder: type[json.JSONDecoder] | None = None
        self.viewsets_command_middleware: list[CommandMiddleware] = []
        self.viewsets_auth_processors: list[AuthBackend] = []
        self.default_action_configuration: dict[Any, Any] = {}
        self.viewsets_register_muxws: bool = True
        """
        Whether a viewset is reachable over the muxws transport (see fastapi_viewsets/mux_ws/) when
        it does not say either way itself. `route_viewset(register_muxws=...)` overrides this per
        viewset, and `muxws_endpoint(register_muxws=...)` overrides it again per endpoint - each
        level only decides for the levels below it that left the question open (None). Read at
        decoration time, so it must be set before any viewset class is decorated.
        """
        self.viewsets_security_scheme: SecurityBase | None = None
        """
        Purely for OpenAPI/Swagger discoverability (a lock icon + testable Authorize flow) - attached
        as an extra sub-dependency alongside the per-route Middleware.depends() bridge (see
        route_viewset.py), not otherwise consumed by this library. Must be set before any viewset
        class is decorated with route_viewset (same existing constraint as
        disable_response_model=bool(settings.viewsets_command_middleware) already has).
        """


settings = Settings()
