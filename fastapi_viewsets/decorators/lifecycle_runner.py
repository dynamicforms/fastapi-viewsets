import functools

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, TYPE_CHECKING, TypeVar

from ..action_configuration import extra_middlewares_for, resolve_action_configuration
from ..conf import settings
from ..context import Context, get_shared_context
from ..middleware import ActionExecution, ExecutionRef, run_command_chain, run_execution_chain, ViewSetResult

if TYPE_CHECKING:
    from fastapi import Request

T = TypeVar("T")

LifecycleType = Literal["singleton", "per-request", "instance-key"]

DEFERRED_ACTIONS_ATTRIBUTE = "__viewset_deferred_actions__"
"""
The class attribute holding the names of the actions whose method on that class hands the call to
an executor instead of executing it (`celery_viewset_client` sets it). `lifecycle_runner` runs no
`Middleware.execute()` around such a method, its override in a subclass included; the executor's
`lifecycle_runner` runs it around the real execution.
"""


@dataclass(frozen=True)
class CurrentCall:
    """The call `lifecycle_runner` is running in this task, for a method that hands it on."""

    context: Context
    request: "Request | None"
    execution: ActionExecution | None
    """Set while the action executes in this process; None while a deferred action hands it on."""
    parent: ExecutionRef | None

    def parent_of_dispatch(self) -> ExecutionRef | None:
        """The `parent` of an action handed to an executor now: this execution when one runs, else
        this call's own parent (the deferred method handing its own call on)."""
        return self.execution.ref() if self.execution is not None else self.parent


_current_call: ContextVar[CurrentCall | None] = ContextVar("viewsets_current_call", default=None)


execution_translator: ContextVar[Callable[[BaseException], BaseException] | None] = ContextVar(
    "viewsets_execution_translator", default=None
)
"""
Set by an executor around the executions it runs: maps an exception out of each `call_next()` of
`Middleware.execute()` to the exception raised in its place. None for an in-process execution.
"""


def current_call() -> CurrentCall | None:
    """The call `lifecycle_runner` is running in this task; None outside any call."""
    return _current_call.get()


def effective_middlewares(action_configuration: dict) -> list:
    return settings.viewsets_command_middleware + extra_middlewares_for(
        action_configuration, settings.viewsets_command_middleware
    )


async def lifecycle_runner(
    original_endpoint: Callable,
    singleton_instance: T,
    cls: type[T],
    lifecycle: LifecycleType = "singleton",
    *args,
    response: Any = None,
    needs_context: bool = False,
    request: "Request | None" = None,
    forwards_request: bool = False,
    context: Context | None = None,
    parent: ExecutionRef | None = None,
    **kwargs,
):
    """
    `response` is reserved for the command middleware chain (see below) - callers that have no
    live Response object (e.g. `celery_viewset_server`, running in a worker process) simply omit
    it, and the chain is never invoked - the worker just executes the action and returns the plain
    result; command middleware only ever runs once that result is back in the FastAPI process.

    `context` is the call's data when the caller already has it (`celery_viewset_server`, from the
    task payload); without it a Context is built via settings.viewsets_context_processors against
    whichever instance ends up handling this lifecycle. Either way it is bound to this action's
    configuration and is the context the middleware chains read. `needs_context` injects it into
    kwargs as `context`, for a method declaring that parameter. `forwards_request` passes `request`
    on to a method that declares a `request` parameter. `parent` is the execution in which the call
    was dispatched.

    `Middleware.execute()` of the effective middlewares runs around the method call itself, unless
    the action is deferred on `cls` (`DEFERRED_ACTIONS_ATTRIBUTE`). While the method runs, the call
    is readable through `current_call()`.
    """

    if forwards_request:
        kwargs["request"] = request

    action = original_endpoint.__name__
    action_configuration: dict = {}
    chain_context: Context | None = None

    async def inject_context(req_instance: Any) -> None:
        nonlocal action_configuration, chain_context
        action_configuration = resolve_action_configuration(cls, action)
        if context is None:
            chain_context = await get_shared_context(request, req_instance, action_configuration, action)
        else:
            chain_context = Context(context.raw(), action_configuration=action_configuration, action_name=action)
        if needs_context:
            kwargs["context"] = chain_context

    async def call_bound(req_instance: Any) -> Any:
        bound_method = getattr(req_instance, action, None)
        method = bound_method if bound_method is not None else functools.partial(original_endpoint, req_instance)
        if action in getattr(cls, DEFERRED_ACTIONS_ATTRIBUTE, ()):
            marker = _current_call.set(CurrentCall(chain_context, request, None, parent))
            try:
                return await method(*args, **kwargs)
            finally:
                _current_call.reset(marker)
        execution = ActionExecution(cls, req_instance, action, args, dict(kwargs), chain_context, request, parent)
        marker = _current_call.set(CurrentCall(chain_context, request, execution, parent))
        try:
            return await run_execution_chain(
                effective_middlewares(action_configuration),
                execution,
                lambda: method(*args, **kwargs),
                _translate=execution_translator.get(),
            )
        finally:
            _current_call.reset(marker)

    async def execute(req_instance: Any) -> Any:
        if response is None:
            # Worker-only (celery_viewset_server) invocation - no live Response to shape, so the
            # command middleware chain (whose whole job is producing/mutating response-level side
            # effects) never runs here; just execute and return the plain result.
            return await call_bound(req_instance)

        async def final_handler() -> ViewSetResult:
            raw = await call_bound(req_instance)
            # An endpoint may return a ViewSetResult directly (see ViewSetResult in
            # fastapi_viewsets/middleware) to reach status_code/headers/cookies itself - passed
            # through as-is.
            return raw if isinstance(raw, ViewSetResult) else ViewSetResult(body=raw)

        result = await run_command_chain(
            effective_middlewares(action_configuration), request, req_instance, chain_context, final_handler
        )
        for key, value in result.headers.items():
            response.headers[key] = str(value)
        for key, value in result.cookies.items():
            response.set_cookie(key, str(value))
        if result.status_code is not None:
            response.status_code = result.status_code
        return result.body

    async def run_with_state(req_instance: Any) -> Any:
        if hasattr(req_instance, "load_state"):
            await req_instance.load_state()

        try:
            await inject_context(req_instance)
            return await execute(req_instance)
        finally:
            if hasattr(req_instance, "save_state"):
                await req_instance.save_state()

    if lifecycle == "singleton":
        return await run_with_state(singleton_instance)
    elif lifecycle == "per-request":
        req_instance = cls()
        await inject_context(req_instance)
        return await execute(req_instance)
    elif lifecycle == "instance-key":
        req_instance = cls()
        return await run_with_state(req_instance)

    raise ValueError(f"unknown lifecycle {lifecycle!r}; one of: singleton, per-request, instance-key")
