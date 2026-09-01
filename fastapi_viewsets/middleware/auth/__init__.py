from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING

from fastapi import HTTPException

from ...response_classes import ErrorDetail
from .. import Middleware, ViewSetResult

if TYPE_CHECKING:
    from fastapi import Request

    from ...context import Context


class Session(Middleware):
    """
    Pairs with `auth_context_processor` (see `fastapi_viewsets.context.auth`): that processor only
    ever *gathers* the fact ("user is None" or a real user) by resolving whichever `AuthBackend`
    claimed the request - this is what actually *decides* what to do about it. It doesn't re-derive
    or re-resolve anything itself - it just checks the already-resolved `context.user` that
    `context.auth` produced.

    The whole check lives in `depends()` - bridged onto FastAPI's own `Depends()` by route_viewset,
    so an expired/missing session is rejected before FastAPI even parses the request body.
    `__call__` (the onion command-middleware chain) is therefore a trivial passthrough: if
    `depends()` would have rejected, `__call__`/the onion chain is never even reached at all.

    A viewset (or a specific `perform_*` method) opts out - e.g. a login/signup endpoint that must
    be reachable without a session - via `@action_configuration({Session: False})` (see
    `fastapi_viewsets.action_configuration`); everything else is protected by default, since
    `settings.viewsets_command_middleware` is global and applies to every route.

    401, not 403: an expired/missing/unrecognized session means the credential itself no longer
    establishes who's calling - that's "unauthorized" (401), not "authorized but forbidden" (403),
    which would apply to a *known* caller lacking permission for this specific action.
    """

    async def depends(self, _request: "Request | None", _cls: type, context: "Context") -> None:
        config = self.config_from(context)
        required = True if config is None else bool(config)
        if not required:
            return
        user = await context.user
        if user is None:
            detail = ErrorDetail(message="Session expired or invalid", code="session_expired")
            raise HTTPException(status_code=401, detail=detail.model_dump())

    async def __call__(
        self,
        _request: "Request | None",
        _viewset: Any,
        _context: "Context",
        call_next: Callable[[], Awaitable[ViewSetResult]],
    ) -> ViewSetResult:
        return await call_next()
