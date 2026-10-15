from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Any, Literal, TYPE_CHECKING, TypeVar

from ..action_configuration import extra_middlewares_for, resolve_action_configuration
from ..conf import settings
from ..context import Context, get_shared_context
from ..middleware import run_command_chain, ViewSetResult

if TYPE_CHECKING:
    from fastapi import Request

T = TypeVar("T")

LifecycleType = Literal["singleton", "per-request", "instance-key"]

CELERY_DISPATCH_ATTRIBUTE = "__celery_viewset_dispatch__"
"""
Set to True on the methods `celery_viewset_client` installs. Such a method sends the call to a Celery
worker instead of executing the action, so `lifecycle_runner` does not run the execution hook around
it; the worker's `lifecycle_runner` runs it around the real execution.
"""


@dataclass(frozen=True)
class ActionExecution:
    """What the execution hook receives about the action that is about to execute."""

    viewset_class: type
    action: str
    """The name of the viewset method, e.g. `"create"`."""
    context: Context | None
    """The call's `Context`. None where none was built: in a Celery worker, for an action that does
    not declare a `context` parameter."""
    request: "Request | None"
    """The live request; None in a Celery worker."""


ExecutionHook = Callable[[ActionExecution], AbstractAsyncContextManager[Any]]

_execution_hook: ExecutionHook | None = None


def set_execution_hook(hook: ExecutionHook | None) -> None:
    """
    Register a callable returning an async context manager that is entered right before the action
    executes and exited when it has returned or raised. None removes it.

    It runs where the action executes: in the FastAPI process for an in-process action, inside the
    command middleware chain, and in the Celery worker (`celery_viewset_server`) for a
    `celery_viewset` action, whose FastAPI side only dispatches and does not run it. An exception the
    action raises reaches the context manager's `__aexit__` and propagates after it; an exception
    raised by the context manager itself propagates in place of the action's outcome, and one raised
    on entry keeps the action from running.
    """
    global _execution_hook
    _execution_hook = hook


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
    **kwargs,
):
    """
    `response` is reserved for the command middleware chain (see below) - callers that have no
    live Response object (e.g. `celery_viewset_server`, running in a worker process) simply omit
    it, and the chain is never invoked - the worker just executes the action and returns the plain
    result; command middleware only ever runs once that result is back in the FastAPI process.

    `needs_context`/`request` are reserved for the context-processor mechanism (see
    fastapi_viewsets/context): when needs_context is True, a Context is built (via
    settings.viewsets_context_processors) against whichever instance ends up handling this
    lifecycle and injected into kwargs as `context` before the endpoint runs. Callers that
    already have a resolved context (e.g. `celery_viewset_server`, reconstructing it from a
    Celery task payload) simply omit needs_context and pass `context` directly in kwargs.
    `forwards_request` passes `request` on to an endpoint that declares a `request` parameter.

    The execution hook (see `set_execution_hook`) is entered around the method call itself, unless the
    method is a `celery_viewset_client` dispatch stub (`CELERY_DISPATCH_ATTRIBUTE`).
    """

    if forwards_request:
        kwargs["request"] = request

    action_configuration: dict = {}
    chain_context = kwargs.get("context")

    async def inject_context(req_instance: Any) -> None:
        nonlocal action_configuration, chain_context
        # Resolved whether or not the route declares a `context` param: execute() needs it to
        # compute any extra per-call middleware (see extra_middlewares_for), and the command
        # middleware chain reads the context it is part of. Neither is built in the celery-worker
        # path (no `response`), where command middleware never runs.
        action_configuration = resolve_action_configuration(cls, original_endpoint.__name__)
        if needs_context or response is not None:
            chain_context = await get_shared_context(
                request, req_instance, action_configuration, original_endpoint.__name__
            )
        if needs_context:
            kwargs["context"] = chain_context

    async def call_bound(req_instance: Any) -> Any:
        bound_method = getattr(req_instance, original_endpoint.__name__, None)
        if bound_method is not None:
            call = lambda: bound_method(*args, **kwargs)
        else:
            call = lambda: original_endpoint(req_instance, *args, **kwargs)
        hook = _execution_hook
        if hook is None or getattr(bound_method or original_endpoint, CELERY_DISPATCH_ATTRIBUTE, False):
            return await call()
        async with hook(ActionExecution(cls, original_endpoint.__name__, chain_context, request)):
            return await call()

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

        effective_middlewares = settings.viewsets_command_middleware + extra_middlewares_for(
            action_configuration, settings.viewsets_command_middleware
        )
        result = await run_command_chain(effective_middlewares, request, req_instance, chain_context, final_handler)
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
