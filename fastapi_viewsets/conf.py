import json

from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

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


settings = Settings()
