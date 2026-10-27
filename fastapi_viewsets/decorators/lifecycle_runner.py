import functools

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Literal, TYPE_CHECKING, TypeVar

from ..action_configuration import extra_middlewares_for, resolve_action_configuration
from ..conf import settings
from ..context import Context, get_shared_context
from ..middleware import (
    ActionExecution,
    apply_cookies,
    ExecutionRef,
    run_command_chain,
    run_execution_chain,
    ViewSetResult,
)

if TYPE_CHECKING:
    from fastapi import Request

T = TypeVar("T")

LifecycleType = Literal["singleton", "per-request", "instance-key"]

DEFERRED_ACTIONS_ATTRIBUTE = "__viewset_deferred_actions__"
"""
Class attribute naming the actions whose method hands the call to an executor (set by `celery_viewset_client`).
`lifecycle_runner` runs no `Middleware.execute()` around them; the executor's run does.
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
        """The `parent` of an action dispatched now: this execution when one runs, else this call's parent."""
        return self.execution.ref() if self.execution is not None else self.parent


_current_call: ContextVar[CurrentCall | None] = ContextVar("viewsets_current_call", default=None)


execution_translator: ContextVar[Callable[[BaseException], BaseException] | None] = ContextVar(
    "viewsets_execution_translator", default=None
)
"""Set by an executor: maps an exception out of each `execute()` step to the one raised instead; None in-process."""


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
    Runs one action call on the instance `lifecycle` selects, inside the middleware chains.

    Without `response` (an executor's process) the command chain is skipped and the plain result returned.
    `context` is used when given (from a task payload), else built by the context processors; `needs_context`
    and `forwards_request` inject `context` / `request` into the method. `current_call()` exposes the call.
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
            # Executor process: no Response to shape, so no command chain.
            return await call_bound(req_instance)

        async def final_handler() -> ViewSetResult:
            raw = await call_bound(req_instance)
            # An endpoint returning a ViewSetResult itself is passed through.
            return raw if isinstance(raw, ViewSetResult) else ViewSetResult(body=raw)

        result = await run_command_chain(
            effective_middlewares(action_configuration), request, req_instance, chain_context, final_handler
        )
        for key, value in result.headers.items():
            response.headers[key] = str(value)
        apply_cookies(response, result.cookies)
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
