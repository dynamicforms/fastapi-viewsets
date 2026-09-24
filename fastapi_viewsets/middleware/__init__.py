from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Generic, get_args, get_origin, TYPE_CHECKING, TypeVar

from ..context import Context, SerializableObject, to_jsonable

if TYPE_CHECKING:
    from fastapi import Request

T = TypeVar("T")


@dataclass
class ViewSetResult(SerializableObject, Generic[T]):
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

    A registered route endpoint (a mixin-provided one, or a custom `__router` method - never a
    `perform_*` hook, which is called by the actual route method and never registered as a route
    itself) may also return a `ViewSetResult` directly instead of a plain body, getting the same
    `status_code`/`headers`/`cookies` access normally reserved for command middleware (e.g. a
    redirect: `ViewSetResult(body=None, status_code=302, headers={"Location": url})`) -
    `lifecycle_runner.final_handler` passes it through unchanged. Declare the return type as
    `ViewSetResult[X]` (X being whatever the wire body actually is) rather than bare `X` -
    `route_viewset`/`build_schema` unwrap this to `X` for the OpenAPI response_model and FastAPI's
    own response validation (see unwrap_viewset_result_type below), so the two forms
    document/validate identically.

    Inherits `SerializableObject` so it survives the Celery/Redis boundary: a
    `celery_viewset`-dispatched action's worker-side return value is passed through
    `serialize_value()`/`deserialize_value()` (see fastapi_viewsets.context) exactly like any other
    `SerializableObject`, the same mechanism `LazyObject`/`Context` values already rely on. Only
    `__serialize__`/`__deserialize__` are overridden here - a `ViewSetResult` is never itself placed
    in a `Context` and never awaited, so `SerializableObject`'s `value`/`__await__`/`__init__` go
    unused, the same way `LazyObject` replaces those with its own.
    """

    body: T
    headers: dict[str, Any] = field(default_factory=dict)
    cookies: dict[str, Any] = field(default_factory=dict)
    status_code: int | None = None

    def __serialize__(self) -> Any:
        return {
            "body": to_jsonable(self.body),
            "headers": self.headers,
            "cookies": self.cookies,
            "status_code": self.status_code,
        }

    @classmethod
    def __deserialize__(cls, data: Any) -> "ViewSetResult":
        return cls(body=data["body"], headers=data["headers"], cookies=data["cookies"], status_code=data["status_code"])


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
    middleware needs to carry its own configuration/state (see `middleware.auth.Session` for a
    concrete example) - a bare function would need extra indirection (closures, partials) for that.
    """

    modifies_response_shape: bool = False
    """
    Whether this middleware can change the shape of ViewSetResult.body (strip/rename/restructure
    fields, not just add side-channel headers/cookies or a status code) - see
    `any_modifies_response_shape()` below. False by default: typical middleware (auth, rate
    limiting, sessions) only inspects the result or attaches headers/cookies, so the endpoint's
    declared return type stays a trustworthy response_model. Override to True on a middleware whose
    __call__ actually reassigns `result.body` to something that no longer matches the original
    return annotation - otherwise FastAPI's response_model validation will coerce the reshaped body
    back towards the original model (e.g. reintroducing a stripped field with its default value).
    A plain function middleware declares this the same way, by setting the attribute on the
    function object: `my_middleware.modifies_response_shape = True`.
    """

    def config_from(self, context: Context) -> Any:
        """
        This middleware's own @action_configuration value for the current call (see
        fastapi_viewsets/action_configuration.py), or None if nothing was configured for it.
        Shorthand for `context.configuration_for(type(self))`.
        """
        return context.configuration_for(type(self))

    async def depends(self, _request: "Request", _cls: type, _context: Context) -> None:
        """
        Optional early-gate hook, bridged onto FastAPI's own native Depends() by route_viewset -
        runs before FastAPI even parses the request body. Raise HTTPException to reject; return
        normally to allow. Default is a no-op: a middleware that doesn't override this only runs in
        the onion chain below, exactly as if this method didn't exist.

        `cls` is always the viewset *class*, never an instance - for `per-request`/`instance-key`
        lifecycles no instance exists yet this early (uniform across all 3 lifecycle modes, rather
        than sometimes a real instance and sometimes not, depending on which one). `context` is
        real and usable here (see `fastapi_viewsets.context.get_shared_context`) - built as early as
        this method can possibly run, which for `per-request`/`instance-key` lifecycles is *before*
        `load_state()` (a real, accepted change in ordering - no shipped context processor reads
        viewset instance state, so this doesn't affect anything today).

        Put here *only* logic that might actually reject - there's no benefit to running
        unconditional work (e.g. enriching context with no reject path - see
        `middleware.auth.authorization.Authorization`, which splits across both methods for exactly
        this reason) before body parsing, so that kind of work stays in `__call__` as it does today.
        For middleware whose entire job is reject-or-allow with nothing unconditional (`Session`,
        `RateLimiter`), `__call__` correctly becomes a trivial `return await call_next()`: `depends()`
        already made the only decision that matters, and if it rejected, `__call__`/the onion chain
        is never even reached at all (the exception already propagated out via FastAPI's own
        `Depends()` resolution, before our wrapper/lifecycle_runner ever starts) - so nothing runs
        twice.
        """
        return None

    @abstractmethod
    async def __call__(
        self,
        request: "Request | None",
        viewset: Any,
        context: Context,
        call_next: Callable[[], Awaitable[ViewSetResult]],
    ) -> ViewSetResult:
        raise NotImplementedError


def any_modifies_response_shape(middlewares: list) -> bool:
    """
    Whether any entry in `middlewares` (a mix of Middleware instances and/or plain functions, as
    settings.viewsets_command_middleware holds) declares modifies_response_shape=True. A bare
    function that never set the attribute is treated as False, same default as the Middleware
    class - see Middleware.modifies_response_shape above. Used by route_viewset/build_schema to
    decide whether an endpoint's declared response_model can still be trusted for OpenAPI docs and
    FastAPI's own response validation.
    """
    return any(getattr(middleware, "modifies_response_shape", False) for middleware in middlewares)


def unwrap_viewset_result_type(annotation: Any) -> Any:
    """
    An endpoint declared `-> ViewSetResult[X]` (see ViewSetResult above) documents/validates as
    `X` - the wire body a client actually receives, not the side-channel wrapper the endpoint
    returns internally to reach `status_code`/`headers`/`cookies`. `route_viewset`/`build_schema`
    call this wherever a return annotation becomes an OpenAPI response_model, so `-> ViewSetResult[X]`
    and plain `-> X` document/validate identically. Anything that isn't `ViewSetResult[...]` passes
    through unchanged.
    """
    if get_origin(annotation) is ViewSetResult:
        args = get_args(annotation)
        return args[0] if args else None
    return annotation


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
