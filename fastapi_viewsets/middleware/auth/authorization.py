import inspect

from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING

from ...exceptions import NotAuthorizedError
from .. import Middleware, ViewSetResult

if TYPE_CHECKING:
    from fastapi import Request

    from ...context import Context


class Authorization(Middleware):
    """
    Command middleware for authorization, configured per action with `@action_configuration`:

    - a callable config is evaluated as `check(request, cls, context)` (sync or async) in `depends()`;
      a falsy result rejects the request with 403 before the body is parsed;
    - any other value (or `None` when unconfigured) is enforced by nothing and becomes
      `context.authorization`, for `perform_*` to check and raise 403 itself.

        @action_configuration({Authorization: lambda request, cls, context: is_admin(context)})
        class AdminOnlyViewSet(...): ...
    """

    async def depends(self, request: "Request | None", cls: type, context: "Context") -> None:
        config = self.config_from(context)
        if callable(config):
            allowed = config(request, cls, context)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                raise NotAuthorizedError()

    async def __call__(
        self,
        _request: "Request | None",
        _viewset: Any,
        context: "Context",
        call_next: Callable[[], Awaitable[ViewSetResult]],
    ) -> ViewSetResult:
        config = self.config_from(context)
        if not callable(config):
            context.set("authorization", config)
        return await call_next()
