from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING

from ...exceptions import SessionExpiredError
from .. import Middleware, ViewSetResult

if TYPE_CHECKING:
    from fastapi import Request

    from ...context import Context


class Session(Middleware):
    """
    Rejects a call without a user (`context.user` from `auth_context_processor`) with 401
    `SessionExpiredError`, in `depends()`, before the body is parsed.

    Applies to every route; opt out with `@action_configuration({Session: False})` (e.g. login).
    """

    async def depends(self, _request: "Request | None", _cls: type, context: "Context") -> None:
        config = self.config_from(context)
        required = True if config is None else bool(config)
        if not required:
            return
        user = await context.user
        if user is None:
            raise SessionExpiredError()

    async def __call__(
        self,
        _request: "Request | None",
        _viewset: Any,
        _context: "Context",
        call_next: Callable[[], Awaitable[ViewSetResult]],
    ) -> ViewSetResult:
        return await call_next()
