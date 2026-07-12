import inspect

from collections.abc import Awaitable, Callable
from typing import Any, TYPE_CHECKING

from .. import Middleware, ViewSetResult

if TYPE_CHECKING:
    from fastapi import Request

    from ...context import Context


class Authorization(Middleware):
    """
    Command middleware for authorization (is this caller allowed to do *this*) - complements
    `Session` (authentication: who's calling). Configured via `@action_configuration` (see
    `fastapi_viewsets.action_configuration`), keyed by this class. The configured value has two
    roles at once, depending on its shape:

    - If it's callable, it's evaluated as `check(request, viewset, context)` (sync or async) - a
      falsy result rejects the request with 403 before `perform_*` ever runs. Use this for checks
      that don't need a specific record (e.g. a role check).
    - Either way (callable or not, or even unconfigured), the resolved value is made available as
      `context.authorization` - `perform_*` can inspect it once it has whatever record-specific
      info a check needs (e.g. the object a `pk` resolved to) and reject itself
      (`raise HTTPException(403, ...)`) if it decides to. This is the "no hook in the middleware
      layer needed" case - just raise, same as anywhere else in the pipeline (see
      docs/guide/action-configuration.md's "Rejecting a request: just raise").

    Unconfigured (no `@action_configuration` entry for `Authorization` at all): nothing is
    enforced, `context.authorization` resolves to `None`.

        @action_configuration({Authorization: lambda request, viewset, context: is_admin(context)})
        class AdminOnlyViewSet(...): ...

        @action_configuration({Authorization: "owner-only"})
        class ItemViewSet(...):
            async def perform_retrieve(self, context: Context, pk: int) -> Item:
                item = await fetch(pk)
                if await context.authorization == "owner-only" and item.owner != await context.user:
                    raise HTTPException(403, "Not your item")
                return item
    """

    async def __call__(
        self,
        request: "Request | None",
        viewset: Any,
        context: "Context",
        call_next: Callable[[], Awaitable[ViewSetResult]],
    ) -> ViewSetResult:
        config = self.config_from(context)
        context.set("authorization", config)
        if callable(config):
            allowed = config(request, viewset, context)
            if inspect.isawaitable(allowed):
                allowed = await allowed
            if not allowed:
                return ViewSetResult(
                    body={"detail": "Not authorized to perform this action"}, status_code=403
                )
        return await call_next()
