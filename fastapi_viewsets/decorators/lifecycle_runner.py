from collections.abc import Callable
from typing import Any, Literal, TYPE_CHECKING, TypeVar

from ..action_configuration import extra_middlewares_for, resolve_action_configuration
from ..conf import settings
from ..context import get_shared_context
from ..middleware import run_command_chain, ViewSetResult

if TYPE_CHECKING:
    from fastapi import Request

T = TypeVar("T")

LifecycleType = Literal["singleton", "per-request", "instance-key"]


async def lifecycle_runner(
    original_endpoint: Callable,
    singleton_instance: T,
    cls: type[T],
    lifecycle: LifecycleType = "singleton",
    *args,
    response: Any = None,
    needs_context: bool = False,
    request: "Request | None" = None,
    **kwargs
):
    """
    `response` is reserved for the command middleware chain (see below) - callers that have no
    live Response object (e.g. `celery_viewset_server`, running in a worker process) simply omit
    it, and the chain is never invoked - the worker just executes the action and returns the plain
    result; command middleware only ever runs once that result is back in the FastAPI process,
    same as the old `finalize_response` hook it replaces.

    `needs_context`/`request` are reserved for the context-processor mechanism (see
    fastapi_viewsets/context.py): when needs_context is True, a Context is built (via
    settings.viewsets_context_processors) against whichever instance ends up handling this
    lifecycle and injected into kwargs as `context` before the endpoint runs. Callers that
    already have a resolved context (e.g. `celery_viewset_server`, reconstructing it from a
    Celery task payload) simply omit needs_context and pass `context` directly in kwargs.
    """

    action_configuration: dict = {}

    async def inject_context(req_instance: Any) -> None:
        nonlocal action_configuration
        # Resolved unconditionally (not just when needs_context) - execute() needs it to compute
        # any extra per-call middleware (see extra_middlewares_for) regardless of whether this
        # route even declares a `context` param. Harmless in the celery-worker path: execute()
        # returns before touching it there, since command middleware never runs in the worker.
        action_configuration = resolve_action_configuration(cls, original_endpoint.__name__)
        if needs_context:
            kwargs["context"] = await get_shared_context(
                request, req_instance, action_configuration, original_endpoint.__name__
            )

    async def call_bound(req_instance: Any) -> Any:
        bound_method = getattr(req_instance, original_endpoint.__name__, None)
        if bound_method is not None:
            return await bound_method(*args, **kwargs)
        return await original_endpoint(req_instance, *args, **kwargs)

    async def execute(req_instance: Any) -> Any:
        if response is None:
            # Worker-only (celery_viewset_server) invocation - no live Response to shape, so the
            # command middleware chain (whose whole job is producing/mutating response-level side
            # effects) never runs here; just execute and return the plain result.
            return await call_bound(req_instance)

        async def final_handler() -> ViewSetResult:
            return ViewSetResult(body=await call_bound(req_instance))

        effective_middlewares = settings.viewsets_command_middleware + extra_middlewares_for(
            action_configuration, settings.viewsets_command_middleware
        )
        result = await run_command_chain(
            effective_middlewares, request, req_instance, kwargs.get("context"), final_handler
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

    return await original_endpoint(*args, **kwargs)
