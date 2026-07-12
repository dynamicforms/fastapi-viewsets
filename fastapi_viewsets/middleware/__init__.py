from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

from ..context import Context

if TYPE_CHECKING:
    from fastapi import Request


@dataclass
class ViewSetResult:
    """
    Transport-agnostic result of running the command middleware chain (see run_command_chain()
    below). `body` is the actual domain value (whatever perform_* returned); `headers`/`cookies`
    are side-channel metadata a middleware can attach - e.g. a session middleware setting a value
    the frontend should remember. `status_code`, when set, lets a middleware short-circuit the
    chain with a non-200 result (e.g. 401 for an expired session) without ever calling `perform_*`.
    What this all actually becomes on the wire is up to the transport adapter: the HTTP adapter
    (route_viewset.py/lifecycle_runner.py) maps `headers` onto real response headers, `cookies`
    onto real Set-Cookie, and `status_code` onto the real Response's status code; a future WS
    adapter would fold all three into the outgoing message payload as plain JSON keys instead (no
    real headers/cookies/status line exist over WS).
    """

    body: Any
    headers: dict[str, Any] = field(default_factory=dict)
    cookies: dict[str, Any] = field(default_factory=dict)
    status_code: int | None = None


CommandMiddleware = Callable[
    ["Request | None", Any, Context, Callable[[], Awaitable[ViewSetResult]]],
    Awaitable[ViewSetResult],
]
"""
async def middleware(request, viewset, context, call_next) -> ViewSetResult: ...

An onion-style chain around the actual perform_*/endpoint execution, configured via
settings.viewsets_command_middleware (see fastapi_viewsets/conf.py). `call_next()` (no arguments -
context and the viewset instance are the only mutable structures in the pipeline, so a middleware
that wants to influence what happens next mutates `context` in place rather than threading a new
one through) invokes the next middleware, or the actual execution at the innermost layer. This
replaces the old `finalize_response` hook: a middleware can inspect/replace the ViewSetResult
`call_next()` returns and mutate its `headers`/`cookies` before returning it back up the chain.
"""


class Middleware(ABC):
    """
    Class-based alternative to a plain CommandMiddleware function. Implement `__call__` with the
    exact same signature a function would have - `run_command_chain` below calls
    `middlewares[index](request, viewset, context, call_next)` either way, so a `Middleware`
    instance is a drop-in `CommandMiddleware`. Reach for a class instead of a function when the
    middleware needs to carry its own configuration/state (see `context.auth.Session` for a
    concrete example) - a bare function would need extra indirection (closures, partials) for that.
    """

    @abstractmethod
    async def __call__(
        self,
        request: "Request | None",
        viewset: Any,
        context: Context,
        call_next: Callable[[], Awaitable[ViewSetResult]],
    ) -> ViewSetResult:
        raise NotImplementedError


async def run_command_chain(
    middlewares: list[CommandMiddleware],
    request: "Request | None",
    viewset: Any,
    context: Context,
    final_handler: Callable[[], Awaitable[ViewSetResult]],
) -> ViewSetResult:
    """Runs the configured command middleware chain, innermost step being final_handler (the
    actual perform_*/endpoint call, already wrapped as a ViewSetResult)."""

    async def _step(index: int) -> ViewSetResult:
        if index >= len(middlewares):
            return await final_handler()
        return await middlewares[index](request, viewset, context, lambda: _step(index + 1))

    return await _step(0)
