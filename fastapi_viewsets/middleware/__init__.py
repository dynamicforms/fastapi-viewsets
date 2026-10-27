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
    Transport-agnostic result of the command middleware chain: the body plus headers, cookies and an
    optional status code, which each transport maps onto its own response.

    `cookies` maps a name to a `Cookie` (set with all options), a `DeleteCookie` (deleted) or any other
    value (set to `str(value)` with Starlette's defaults); `set_cookie()` / `delete_cookie()` record
    the first two with Starlette's signatures. Cookie options survive the `celery_viewset` boundary.

    A route endpoint may return one directly (e.g. a redirect); declare `-> ViewSetResult[X]`, which
    documents and validates as `X`.
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
`async def middleware(request, viewset, context, call_next) -> ViewSetResult`, one layer of the onion chain
configured in `settings.viewsets_command_middleware`. `call_next()` takes no arguments: a middleware
influences later steps by mutating `context`.
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
    """One execution of a viewset action, as `Middleware.execute()` receives it in whichever process runs it."""

    viewset_class: type
    viewset: Any
    """The instance the action executes on."""
    action: str
    """The name of the viewset method, e.g. `"create"`."""
    args: tuple
    """The positional arguments the method is called with."""
    kwargs: dict[str, Any]
    """The method's keyword arguments; `context` and `request` only where the method declares them."""
    context: Context
    """The call's `Context`, bound to this action's configuration."""
    request: "Request | None"
    """
    The call's request. In an executor's process it is rebuilt from the ASGI scope, without body, `app` or
    `state`. None for an action dispatched outside any call.
    """
    parent: ExecutionRef | None
    """The action in whose execution this action was dispatched; None for an action a request called."""

    def ref(self) -> ExecutionRef:
        """This execution as the `parent` of an action dispatched while it runs."""
        viewset_class = f"{self.viewset_class.__module__}.{self.viewset_class.__qualname__}"
        return ExecutionRef(viewset_class, self.action, self.parent)


@dataclass(frozen=True)
class ActionTransition:
    """A change of state of a deferred action's call, observed outside its execution."""

    kind: Literal["dispatched", "not_run", "interrupted", "failed"]
    """
    - `dispatched`: the call was handed to the executor (in the calling process)
    - `not_run`: the executor will not run the call (`error` is an `ActionDispatchError`)
    - `interrupted`: hard time limit, termination or lost process (`error` is an `ActionInterruptedError`)
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
    Class-based `CommandMiddleware` that can carry its own configuration, with optional hooks around the chain:
    `depends()`, `execute()`, `executor_started()`, `on_transition()`.
    """

    modifies_response_shape: bool = False
    """
    True when `__call__` reshapes `result.body`, so the endpoint's return type no longer serves as
    `response_model` (it would coerce the body back). A function middleware sets it as an attribute.
    `route_viewset` reads it from `settings.viewsets_command_middleware` only, per class.
    """

    def config_from(self, context: Context) -> Any:
        """This middleware's `@action_configuration` value for the current call, or None."""
        return context.configuration_for(type(self))

    async def depends(self, _request: "Request", _cls: type, _context: Context) -> None:
        """
        Optional gate run as a FastAPI dependency, before the body is parsed. Raise `HTTPException` to reject.

        `cls` is the viewset class (no instance exists yet); `context` is built but `load_state()` has not run.
        Put only logic that may reject here; unconditional work belongs in `__call__`.
        """
        return None

    async def execute(self, _execution: ActionExecution, call_next: Callable[[], Awaitable[Any]]) -> Any:
        """
        Optional hook around the viewset method call; `call_next()` returns its result or raises its exception.

        Runs once, in the process that executes the action: for a `celery_viewset` action, the executor's,
        which therefore needs the same `settings.viewsets_command_middleware`. There a soft time limit
        raises `ActionInterruptedError`; a hard limit or termination raises `SystemExit` or nothing.
        """
        return await call_next()

    async def executor_started(self) -> None:
        """
        Optional set-up of an executor's process, awaited once on each event loop before its first use.

        An exception fails the pending execution with `ActionFailedError` (reason `executor`) and the hook
        retries before the next one; before an `on_transition()` call it is only logged.
        """
        return None

    async def on_transition(self, _transition: ActionTransition) -> None:
        """
        Optional listener for `ActionTransition`s: `dispatched` in the calling process, the others in the
        executor's. An exception is logged; in an executor's process each call is bounded to 5 seconds.
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
        The middleware's step of the command chain. For a deferred action `call_next()` may raise
        `ActionDispatchError`, `ActionInterruptedError`, `ActionFailedError`, `ActionOutcomeUnknownError`
        or the action's own `HTTPException`.
        """
        raise NotImplementedError


def any_modifies_response_shape(middlewares: list) -> bool:
    """Whether any middleware (class or function) sets `modifies_response_shape`."""
    return any(getattr(middleware, "modifies_response_shape", False) for middleware in middlewares)


def unwrap_viewset_result_type(annotation: Any) -> Any:
    """`ViewSetResult[X]` to `X`, so it documents and validates like `-> X`; anything else unchanged."""
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
    """Runs the command middleware chain with `final_handler` (the endpoint call) innermost."""

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
    """
    Runs every middleware's `execute()`, the first outermost, around `final_handler`. `_translate`, set by an
    executor, maps an exception out of each step to the one raised in its place.
    """
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
    """Awaits each middleware's `on_transition()`, each bounded to `timeout` seconds; failures are logged."""
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
