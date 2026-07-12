from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING

from .. import Middleware, ViewSetResult

if TYPE_CHECKING:
    from fastapi import Request

    from ...context import Context


class Session(Middleware):
    """
    Command middleware pairing with `auth_context_processor` (see `fastapi_viewsets.context.auth`):
    that processor only ever *gathers* the fact ("user is None" or a real user) by resolving
    whichever `AuthBackend` claimed the request - this middleware is what actually *decides* what
    to do about it, since only command middleware can short-circuit the chain/shape the response.
    It doesn't re-derive or re-resolve anything itself - it just checks the already-resolved
    `context.user` that `context.auth` produced.

    A viewset (or a specific `perform_*` method) opts out - e.g. a login/signup endpoint that must
    be reachable without a session - via `@action_configuration({Session: False})` (see
    `fastapi_viewsets.action_configuration`); everything else is protected by default, since
    `settings.viewsets_command_middleware` is global and applies to every route.

    401, not 403: an expired/missing/unrecognized session means the credential itself no longer
    establishes who's calling - that's "unauthorized" (401), not "authorized but forbidden" (403),
    which would apply to a *known* caller lacking permission for this specific action.
    """

    async def __call__(
        self,
        _request: "Request | None",
        _viewset: Any,
        context: "Context",
        call_next: Callable[[], Awaitable[ViewSetResult]],
    ) -> ViewSetResult:
        config = self.config_from(context)
        required = True if config is None else bool(config)
        if not required:
            return await call_next()
        user = await context.user
        if user is None:
            return ViewSetResult(body={"detail": "Session expired or invalid"}, status_code=401)
        return await call_next()
