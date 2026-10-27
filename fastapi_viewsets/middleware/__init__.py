import asyncio
import logging

from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Generic, get_args, get_origin, Literal, TYPE_CHECKING, TypeVar

from ..context import Context, from_jsonable, SerializableObject, to_jsonable
from .cookies import apply_cookies as apply_cookies, Cookie, DeleteCookie, SameSite

if TYPE_CHECKING:
    from fastapi import Request

    from ..exceptions import DfViewSetError

logger = logging.getLogger(__name__)

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
    onto real Set-Cookie (see `apply_cookies()`), and `status_code` onto the real Response's status
    code; the muxws transport, which serves each command through the same FastAPI app, carries the
    resulting status and headers in the reply's leading headers.

    `cookies` maps a cookie name to one of:

    - a `Cookie`: the cookie with all of its options;
    - a `DeleteCookie`: the cookie is deleted;
    - any other value: the cookie is set to `str(value)` with Starlette's default options.

    `set_cookie()` and `delete_cookie()` record a `Cookie` / `DeleteCookie` under the name, with the
    signatures of Starlette's `Response.set_cookie()` / `Response.delete_cookie()`.

    A registered route endpoint (a mixin-provided one, or a custom `__router` method - never a
    `perform_*` hook, which is called by the actual route method and never registered as a route
    itself) may also return a `ViewSetResult` directly instead of a plain body, getting the same
    `status_code`/`headers`/`cookies` access normally reserved for command middleware (e.g. a
    redirect: `ViewSetResult(body=None, status_code=302, headers={"Location": url})`) -
    `lifecycle_runner.final_handler` passes it through unchanged. Declare the return type as
    `ViewSetResult[X]` (X being whatever the wire body actually is) rather than bare `X` -
    `route_viewset`/`build_schema` unwrap this to `X` for the OpenAPI response_model and FastAPI's
    own response validation (see unwrap_viewset_result_type below), so the two forms
    document/validate identically. `status_code`, `headers` and `cookies` do not change the shape
    of the body, so setting them needs no `Middleware.modifies_response_shape`.

    Inherits `SerializableObject` so it survives the Celery/Redis boundary wherever it is not
    typed; a `celery_viewset`-dispatched action's `ViewSetResult` result crosses with its body
    encoded by the `-> ViewSetResult[X]` annotation (see decorators/celery_viewset/transport.py)
    and its `Cookie`/`DeleteCookie` values with all of their options. Only
    `__serialize__`/`__deserialize__` are overridden here - a `ViewSetResult` is never itself placed
    in a `Context` and never awaited, so `SerializableObject`'s `value`/`__await__`/`__init__` go
    unused, the same way `LazyObject` replaces those with its own.
    """

    body: T
    headers: dict[str, Any] = field(default_factory=dict)
    cookies: dict[str, Any] = field(default_factory=dict)
    status_code: int | None = None

    def set_cookie(
        self,
        key: str,
        value: str = "",
        max_age: int | None = None,
        expires: datetime | str | int | None = None,
        path: str | None = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: SameSite | None = "lax",
        partitioned: bool = False,
    ) -> None:
        """Sets the cookie `key`: records a `Cookie` with these options in `cookies`, replacing
        whatever `cookies` held for `key`."""
        self.cookies[key] = Cookie(value, max_age, expires, path, domain, secure, httponly, samesite, partitioned)

    def delete_cookie(
        self,
        key: str,
        path: str = "/",
        domain: str | None = None,
        secure: bool = False,
        httponly: bool = False,
        samesite: SameSite | None = "lax",
    ) -> None:
        """Deletes the cookie `key`: records a `DeleteCookie` with these options in `cookies`,
        replacing whatever `cookies` held for `key`."""
        self.cookies[key] = DeleteCookie(path, domain, secure, httponly, samesite)

    def __serialize__(self) -> Any:
        return {
            "body": to_jsonable(self.body),
            "headers": self.headers,
            "cookies": to_jsonable(self.cookies),
            "status_code": self.status_code,
        }

    @classmethod
    def __deserialize__(cls, data: Any) -> "ViewSetResult":
        return cls(
            body=from_jsonable(data["body"]),
            headers=data["headers"],
            cookies=from_jsonable(data["cookies"]),
            status_code=data["status_code"],
        )


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
one through) invokes the next middleware, or the actual execution at the innermost layer. A
middleware can inspect/replace the ViewSetResult `call_next()` returns and mutate its
`headers`/`cookies` before returning it back up the chain.
"""


@dataclass(frozen=True)
class ExecutionRef:
    """The action in whose execution another action was dispatched (see `ActionExecution.parent`)."""

    viewset_class: str
    """The viewset class as `module.qualname`."""
    action: str
    parent: "ExecutionRef | None" = None

    def to_jsonable(self) -> dict[str, Any]:
        return {
            "viewset_class": self.viewset_class,
            "action": self.action,
            "parent": self.parent.to_jsonable() if self.parent is not None else None,
        }

    @classmethod
    def from_jsonable(cls, data: dict[str, Any] | None) -> "ExecutionRef | None":
        if data is None:
            return None
        return cls(data["viewset_class"], data["action"], cls.from_jsonable(data["parent"]))


@dataclass(frozen=True)
class ActionExecution:
    """
    One execution of a viewset action, as `Middleware.execute()` receives it. The fields are the
    same wherever the action executes: in the FastAPI process, or in the process of the executor
    a `celery_viewset` action is handed to.
    """

    viewset_class: type
    viewset: Any
    """The instance the action executes on."""
    action: str
    """The name of the viewset method, e.g. `"create"`."""
    args: tuple
    """The positional arguments the method is called with."""
    kwargs: dict[str, Any]
    """The keyword arguments the method is called with: its own parameters, with `context` and
    `request` among them only where the method declares them."""
    context: Context
    """The call's `Context`, whether or not the method declares a `context` parameter. Its
    `configuration_for()` resolves against this action."""
    request: "Request | None"
    """The call's request. In an executor's process it is a `Request` built from the call's ASGI
    scope (method, path, query string, headers, path parameters, client and server address), which
    has no body, no `app` and no `state`. None for an action dispatched outside any call."""
    parent: ExecutionRef | None
    """The action in whose execution this action was dispatched; None for an action a request
    called."""

    def ref(self) -> ExecutionRef:
        """This execution as the `parent` of an action dispatched while it runs."""
        viewset_class = f"{self.viewset_class.__module__}.{self.viewset_class.__qualname__}"
        return ExecutionRef(viewset_class, self.action, self.parent)


@dataclass(frozen=True)
class ActionTransition:
    """
    A change of state of a call to a deferred action that `Middleware.on_transition()` receives,
    observed outside the execution itself.
    """

    kind: Literal["dispatched", "not_run", "interrupted", "failed"]
    """
    - `dispatched`: the call was handed to the executor (in the calling process)
    - `not_run`: the executor will not run the call (`error` is an `ActionDispatchError`)
    - `interrupted`: the executor ended the execution from outside it: a hard time limit, a
      termination, the loss of the executing process (`error` is an `ActionInterruptedError`)
    - `failed`: the executor failed outside the action (`error` is an `ActionFailedError`)
    """
    viewset_class: type
    action: str
    """The name of the viewset method, e.g. `"create"`."""
    context: Context
    """The call's `Context`, bound to the action's configuration."""
    parent: ExecutionRef | None
    """The action in whose execution the call was dispatched; None for an action a request called."""
    error: "DfViewSetError | None"
    """What the caller receives for the call; None for `dispatched`."""


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
    limiting, sessions) only inspects the result or attaches headers/cookies (`set_cookie()`,
    `delete_cookie()`), which are not part of the body, so the endpoint's declared return type
    stays a trustworthy response_model. Override to True on a middleware whose
    __call__ actually reassigns `result.body` to something that no longer matches the original
    return annotation - otherwise FastAPI's response_model validation will coerce the reshaped body
    back towards the original model (e.g. reintroducing a stripped field with its default value).
    A plain function middleware declares this the same way, by setting the attribute on the
    function object: `my_middleware.modifies_response_shape = True`.

    `route_viewset` reads it from the entries of `settings.viewsets_command_middleware` when it
    decorates a class, and one entry declaring it drops the response_model of every route of that
    class. An instance `@action_configuration` injects is not consulted.
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
        runs before FastAPI parses the request body. Raise HTTPException to reject; return
        normally to allow. Default is a no-op: a middleware that doesn't override this only runs in
        the onion chain below, exactly as if this method didn't exist.

        `cls` is always the viewset *class*, never an instance - for `per-request`/`instance-key`
        lifecycles no instance exists yet this early. `context` is real and usable here (see
        `fastapi_viewsets.context.get_shared_context`) - built as early as this method can possibly
        run, which for `per-request`/`instance-key` lifecycles is *before* `load_state()`; no shipped
        context processor reads viewset instance state.

        Put here *only* logic that may reject. Unconditional work (e.g. enriching context with no
        reject path) belongs in `__call__`; `middleware.auth.authorization.Authorization` splits
        across both methods this way.
        For middleware whose entire job is reject-or-allow with nothing unconditional (`Session`,
        `RateLimiter`), `__call__` is `return await call_next()`: `depends()` makes the only
        decision, and if it rejects, `__call__`/the onion chain is not reached (the exception
        propagates out via FastAPI's own `Depends()` resolution, before the wrapper/lifecycle_runner
        starts) - so nothing runs twice.
        """
        return None

    async def execute(self, _execution: ActionExecution, call_next: Callable[[], Awaitable[Any]]) -> Any:
        """
        Optional hook around the execution of the action itself. `call_next()` runs the next
        middleware's `execute()`, or the viewset method at the innermost layer, and returns what the
        method returned; an exception the method raises propagates out of it. The default passes
        straight through.

        It runs once per execution, where the action executes: in the FastAPI process for an
        in-process action, inside the command middleware chain (below every `__call__`), and in the
        executor's process for a deferred (`celery_viewset`) action, whose FastAPI-side method only
        hands the call over. The executor's process therefore needs the same
        `settings.viewsets_command_middleware`.

        In an executor's process `call_next()` raises no exception type of the executor itself. An
        execution the executor interrupts from inside (a soft time limit) reaches `call_next()` as
        `ActionInterruptedError`, whether the action was running or suspended at an `await`; the
        action itself sees `asyncio.CancelledError` in the latter case. An execution the executor
        ends from outside (a hard time limit, a termination, a lost process) ends its process:
        `call_next()` raises `SystemExit` where the process is stopped with SIGTERM and has time to
        unwind, whether the action was running or suspended at an `await`, and nothing at all where it
        is killed. `on_transition()` reports it either way: the supervisor of a prefork pool child, or
        the exiting process itself where no supervisor outlives it. In a
        gevent pool a hard time limit ends the execution only and reaches `call_next()` as
        `ActionInterruptedError`.
        """
        return await call_next()

    async def executor_started(self) -> None:
        """
        Optional hook for setting up an executor's process (`celery_viewset_server`), e.g. the
        clients a middleware uses there. A process runs `on_transition()` calls on an event loop of
        their own and executions on the loop of the thread that runs them (one loop per pool thread
        in a threads pool); the hook is awaited once on each loop the process uses, before the
        loop's first execution or its first `on_transition()` call. Every entry of
        `settings.viewsets_command_middleware` that defines it is awaited, in list order. An
        exception before an execution fails that execution with `ActionFailedError` (reason
        `executor`) and the hook runs again before the loop's next one; an exception before an
        `on_transition()` call is logged. The default does nothing.
        """
        return None

    async def on_transition(self, _transition: ActionTransition) -> None:
        """
        Optional hook that receives the changes of state of a call to a deferred action that happen
        outside its execution (see `ActionTransition.kind`). It runs on the effective middlewares of
        the call's action: `dispatched` in the calling process once the executor accepted the call,
        the other kinds in the executor's process after the caller was sent `transition.error`. An
        exception it raises is logged and changes nothing; in an executor's process each call is
        bounded to 5 seconds. The default does nothing.
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
        """
        The middleware's step of the command chain. For a deferred action `call_next()` returns
        the result the executor sent back, or raises one of: `ActionDispatchError` (the action did
        not run), `ActionInterruptedError` (the executor ended its execution), `ActionFailedError`
        (it failed with an error that is not an `HTTPException`), the `HTTPException` the action
        raised (a `DfViewSetError` of a class `fastapi_viewsets.exceptions` defines keeps its class,
        `code`, `params` and headers), or an `ActionOutcomeUnknownError` (the caller stopped
        waiting).
        """
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


async def run_execution_chain(
    middlewares: list[CommandMiddleware],
    execution: ActionExecution,
    final_handler: Callable[[], Awaitable[Any]],
    *,
    _translate: Callable[[BaseException], BaseException] | None = None,
) -> Any:
    """Runs `execute()` of every middleware that has one, in list order, the first outermost;
    final_handler (the viewset method call) is innermost. `_translate`, set by an executor, maps
    an exception out of every step to the exception raised in its place."""
    executors = [middleware.execute for middleware in middlewares if callable(getattr(middleware, "execute", None))]

    async def _step(index: int) -> Any:
        if index >= len(executors):
            return await final_handler()
        return await executors[index](execution, lambda: _next(index + 1))

    async def _translated_step(index: int) -> Any:
        try:
            return await _step(index)
        except BaseException as error:
            translated = _translate(error)
            if translated is error:
                raise
            raise translated from error

    _next = _step if _translate is None else _translated_step
    return await _next(0)


async def run_executor_started(middlewares: list) -> None:
    """Awaits `executor_started()` of every entry of `middlewares` that defines it, in list order."""
    for middleware in middlewares:
        hook = getattr(middleware, "executor_started", None)
        if callable(hook):
            await hook()


async def run_transition_listeners(
    middlewares: list, transition: ActionTransition, timeout: float | None = None
) -> None:
    """Awaits `on_transition(transition)` of every entry of `middlewares` that defines it, in list
    order, each bounded to `timeout` seconds. An exception or a timeout is logged."""
    for middleware in middlewares:
        listener = getattr(middleware, "on_transition", None)
        if not callable(listener):
            continue
        try:
            await asyncio.wait_for(listener(transition), timeout)
        except Exception:
            logger.exception(
                "on_transition(%s) of %r failed for %s.%s",
                transition.kind,
                middleware,
                transition.viewset_class.__qualname__,
                transition.action,
            )
