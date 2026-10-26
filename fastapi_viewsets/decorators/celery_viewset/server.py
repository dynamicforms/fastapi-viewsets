import asyncio
import atexit
import concurrent.futures
import contextlib
import functools
import inspect
import logging
import os
import queue
import signal
import sys
import threading
import types
import weakref

from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import Any, get_args, get_origin, TYPE_CHECKING, TypeVar, Union

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, TypeAdapter, ValidationError

from ...action_configuration import resolve_action_configuration
from ...conf import settings
from ...context import Context, deserialize_context, from_jsonable
from ...exceptions import ActionDispatchError, ActionFailedError, ActionInterruptedError, DfViewSetError
from ...middleware import ActionTransition, ExecutionRef, run_executor_started, run_transition_listeners
from ..build_schema import build_schema
from ..lifecycle_runner import effective_middlewares, execution_translator, lifecycle_runner, LifecycleType
from ..primary_key_model_helper import (
    create_model_without_pk,
    from_partial_body,
    partial_body_annotation,
    typecast_to_original_model,
)
from .payload import CONTEXT_KWARG, PARENT_KWARG, request_from_jsonable, REQUEST_KWARG, UNTYPED_KWARG
from .result_reader import error_payload, push_payload
from .transport import ActionCodec

if TYPE_CHECKING:
    import redis

    from celery import Celery

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _unwrap_optional(hint):
    """Before Python 3.11, `get_type_hints()` implicitly wraps a None-defaulted parameter's
    annotation in `Optional[...]`, so a hint that is actually a bare model class arrives as
    `Union[Model, None]` and the `inspect.isclass` check below sees a `Union`, not a class."""
    if get_origin(hint) in (Union, types.UnionType):
        args = [arg for arg in get_args(hint) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return hint


def _reconstruct_value(hint, value):
    """Rebuild a dict (or list/tuple of dicts) back into BaseModel instance(s) per `hint`.

    `_reconstruct_kwargs` only unwraps `Optional[...]` at the top level, so a hint arriving here
    may still be `list[Model]` or `Optional[list[Model]]` - handled by recursing into the origin's
    type args rather than requiring `hint` itself to be a bare BaseModel subclass.
    """
    hint = _unwrap_optional(hint)
    if hint is not None and isinstance(value, dict) and inspect.isclass(hint) and issubclass(hint, BaseModel):
        return _validate_model(hint, value)
    if get_origin(hint) in (list, tuple) and isinstance(value, (list, tuple)):
        args = get_args(hint)
        if args:
            return [_reconstruct_value(args[0], item) for item in value]
    return value


def _validate_model(model: type[BaseModel], value: dict) -> BaseModel:
    """`value`, a `model` the FastAPI side dumped by field name, validated as `model`. A create body
    arrives without its primary key: a value whose only error is one missing top-level field is
    validated as `create_model_without_pk(model, field)` and handed on as `model`, as the FastAPI
    side does. Any other error raises."""
    try:
        return model.model_validate(value, by_name=True)
    except ValidationError as error:
        missing = _sole_missing_field(model, error)
        if missing is None:
            raise
    without_pk = _model_without_pk(model, missing)
    return typecast_to_original_model(without_pk.model_validate(value, by_name=True), model)


def _sole_missing_field(model: type[BaseModel], error: ValidationError) -> str | None:
    """The name of the field `error` reports missing, if that is its only error and the field is a
    top-level field of `model`; None otherwise."""
    errors = error.errors()
    if len(errors) != 1 or errors[0]["type"] != "missing" or len(errors[0]["loc"]) != 1:
        return None
    key = errors[0]["loc"][0]
    return next(
        (name for name, field in model.model_fields.items() if key in (name, field.alias, field.validation_alias)),
        None,
    )


_model_without_pk = functools.lru_cache(create_model_without_pk)


def _reconstruct_kwargs(
    original_endpoint, kwargs: dict, cls: type = None, untyped=(), codec: ActionCodec | None = None
) -> dict:
    """The action's keyword arguments from the task's kwargs, each decoded by its parameter's kind
    (see `transport`): a model validated as its model, a partial update body as the partial model the
    FastAPI side validated it with, a typed value validated by its hint, and a value named in
    `untyped` (or of an untyped parameter) with every `SerializableObject` in it rebuilt.

    The call's context, request and parent arrive under the reserved kwargs of `payload`, which the
    task consumes before this runs.
    """
    codec = codec or ActionCodec(cls, original_endpoint)
    result = {}
    for key, value in kwargs.items():
        param = codec.params.get(key)
        kind = param.kind if param is not None else "untyped"
        if kind == "partial":
            # validated as the partial model the FastAPI side validated it with, so the model holds
            # the fields the request stated and no others
            annotation = partial_body_annotation(param.hint, None)
            result[key] = from_partial_body(TypeAdapter(annotation).validate_python(value, by_name=True), param.hint)
        elif kind == "model":
            result[key] = _reconstruct_value(param.hint, value)
        else:
            result[key] = codec.decode_argument(key, value, kind == "typed" and key not in untyped)
    return result


TRANSITION_LISTENER_TIMEOUT = 5.0
"""Seconds one `on_transition()` call may take in an executor's process."""

TRANSITION_DRAIN_TIMEOUT = 10.0
"""Seconds an executor's process waits, when it shuts down, for the transitions it has not reported yet."""

_EXECUTOR_MODULES = frozenset({"celery", "billiard", "kombu"})
_SOFT_LIMIT_SIGNAL = getattr(signal, "SIGUSR1", None)


@functools.cache
def _billiard_exceptions() -> types.SimpleNamespace:
    from billiard.exceptions import SoftTimeLimitExceeded, TimeLimitExceeded, WorkerLostError

    return types.SimpleNamespace(
        SoftTimeLimitExceeded=SoftTimeLimitExceeded,
        TimeLimitExceeded=TimeLimitExceeded,
        WorkerLostError=WorkerLostError,
    )


def _is_executor_error(error: BaseException) -> bool:
    return type(error).__module__.split(".")[0] in _EXECUTOR_MODULES


def _is_pool_timeout(error: BaseException) -> bool:
    """Whether `error` is a `gevent.Timeout`: the gevent pool's hard time limit, raised in the
    execution's greenlet."""
    gevent = sys.modules.get("gevent")
    timeout_class = getattr(gevent, "Timeout", None)
    return timeout_class is not None and isinstance(error, timeout_class)


def _pool_timeout_in(error: BaseException) -> BaseException | None:
    """The `gevent.Timeout` that is `error` or that `error` was raised from, if any."""
    seen = set()
    while error is not None and id(error) not in seen:
        if _is_pool_timeout(error):
            return error
        seen.add(id(error))
        error = error.__cause__ or error.__context__
    return None


class _Run:
    """
    One execution in this process. Maps the exceptions the executor raises into it to this
    package's: a soft time limit and the gevent pool's hard time limit (`gevent.Timeout`) to
    `ActionInterruptedError`, any other celery, billiard or kombu exception to `ActionFailedError`, and
    the `asyncio.CancelledError` of an execution an interruption cancelled to that interruption, or
    to the `SystemExit` / `KeyboardInterrupt` of a process exit.
    """

    def __init__(self, task: str):
        self.task = task
        self.interruption: BaseException | None = None
        self._interrupting: BaseException | None = None

    def interrupted(self, error: BaseException) -> None:
        """Records `error`, which interrupted the loop while the execution was suspended."""
        if isinstance(error, (KeyboardInterrupt, SystemExit)):
            self.interruption = error
            self._interrupting = error
            return
        translated = self.translate(error)
        if isinstance(translated, ActionInterruptedError):
            self.interruption = translated
            self._interrupting = error

    def translate(self, error: BaseException) -> BaseException:
        if self.interruption is not None and (error is self._interrupting or isinstance(error, asyncio.CancelledError)):
            return self.interruption
        if _is_pool_timeout(error):
            return ActionInterruptedError(self.task, "time_limit")
        if _is_executor_error(error):
            if isinstance(error, _billiard_exceptions().SoftTimeLimitExceeded):
                return ActionInterruptedError(self.task, "time_limit")
            return ActionFailedError(self.task)
        return error


@contextlib.contextmanager
def _soft_limit_raises(task: str):
    """
    While active, the soft time limit raises `ActionInterruptedError` at the executing frame in
    place of billiard's `SoftTimeLimitExceeded`. It takes effect on the main thread of a process
    whose soft time limit signal runs billiard's handler, which is restored on exit.
    """
    previous = None
    if _SOFT_LIMIT_SIGNAL is not None and threading.current_thread() is threading.main_thread():
        from billiard.pool import soft_timeout_sighandler

        if signal.getsignal(_SOFT_LIMIT_SIGNAL) is soft_timeout_sighandler:
            previous = soft_timeout_sighandler

    if previous is None:
        yield
        return

    def raise_interrupted(_signum, _frame):
        raise ActionInterruptedError(task, "time_limit")

    signal.signal(_SOFT_LIMIT_SIGNAL, raise_interrupted)
    try:
        yield
    finally:
        signal.signal(_SOFT_LIMIT_SIGNAL, previous)


def _run_to_end(loop: asyncio.AbstractEventLoop, coroutine, run: _Run | None = None) -> Any:
    """Runs `coroutine` on `loop` as a task and returns its result. An exception that interrupts the
    loop while the task is suspended (the executor's time limit, raised from a signal handler) is
    recorded on `run`, cancels the task and runs it until it has unwound, so its `finally` blocks and
    `Middleware.execute()` see the interruption; the exception is then raised."""
    task = loop.create_task(coroutine)
    try:
        return loop.run_until_complete(task)
    except BaseException as error:
        if not task.done():
            if run is not None:
                run.interrupted(error)
            task.cancel()
            with contextlib.suppress(BaseException):  # the interruption is what propagates
                loop.run_until_complete(task)
        raise


def _event_loop() -> asyncio.AbstractEventLoop:
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    if loop.is_running():
        import nest_asyncio

        nest_asyncio.apply()
    return loop


def _supervised() -> bool:
    """Whether this process is a pool child that billiard started, whose supervisor outlives it."""
    from billiard.process import current_process

    return getattr(current_process(), "_parent_pid", None) is not None


_started_pid: int | None = None
_started_loops: "weakref.WeakSet[asyncio.AbstractEventLoop]" = weakref.WeakSet()
_started_lock = threading.Lock()


def _ensure_executor_started(loop: asyncio.AbstractEventLoop, task: str) -> None:
    """Runs `Middleware.executor_started()` once on each event loop this process runs executions on,
    before the loop's first execution. A failure raises `ActionFailedError` (reason `executor`) and
    leaves it to run again before the loop's next execution."""
    global _started_pid
    if _started_pid == os.getpid() and loop in _started_loops:
        return
    with _started_lock:
        if _started_pid != os.getpid():
            # a forked child inherits the parent's set, whose loops it has not set up
            _started_loops.clear()
            _started_pid = os.getpid()
        if loop in _started_loops:
            return
        try:
            _run_to_end(loop, run_executor_started(settings.viewsets_command_middleware))
        except Exception as error:
            logger.exception("executor_started() failed in process %d", os.getpid())
            raise ActionFailedError(task, "executor") from error
        _started_loops.add(loop)


# Transitions observed by the executor outside an execution


@dataclass(frozen=True)
class _Prefix:
    viewset_class: type
    redis_client: "redis.Redis"


_prefixes: dict[str, _Prefix] = {}
_task_actions: dict[str, tuple[type, str]] = {}
_reported: OrderedDict[str, None] = OrderedDict()
_reported_lock = threading.Lock()
_REPORTED_KEPT = 4096


def _action_of(task_name: str | None) -> tuple[type, str, "redis.Redis"] | None:
    """The viewset class, action and redis client of a task named under a registered prefix. A
    task the process did not register resolves to its prefix's class and the name after the prefix."""
    if not task_name:
        return None
    prefix = max((p for p in _prefixes if task_name.startswith(f"{p}.")), key=len, default=None)
    if prefix is None:
        return None
    entry = _prefixes[prefix]
    viewset_class, action = _task_actions.get(task_name, (entry.viewset_class, task_name[len(prefix) + 1 :]))
    return viewset_class, action, entry.redis_client


def _first_report(correlation_id: str) -> bool:
    with _reported_lock:
        if correlation_id in _reported:
            return False
        _reported[correlation_id] = None
        while len(_reported) > _REPORTED_KEPT:
            _reported.popitem(last=False)
        return True


class _TransitionLoop:
    """
    A daemon thread of this process that runs `on_transition()` listeners, one transition at a time,
    on its own event loop, after `Middleware.executor_started()` once. The loop runs only while a
    transition is reported: under gevent's monkey-patching the thread is a greenlet of the thread the
    executions run on, which shares that thread's running event loop.
    """

    def __init__(self):
        self.pid = os.getpid()
        self.loop = asyncio.new_event_loop()
        self.started = False
        self._pending: set[concurrent.futures.Future] = set()
        self._pending_lock = threading.Lock()
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        threading.Thread(target=self._serve, name="fastapi-viewsets-transitions", daemon=True).start()

    def submit(self, transition: ActionTransition) -> concurrent.futures.Future:
        future = concurrent.futures.Future()
        with self._pending_lock:
            self._pending.add(future)
        future.add_done_callback(self._settled)
        self.queue.put((transition, future))
        return future

    def pending(self) -> list[concurrent.futures.Future]:
        with self._pending_lock:
            return list(self._pending)

    def _settled(self, future: concurrent.futures.Future) -> None:
        with self._pending_lock:
            self._pending.discard(future)

    def _serve(self) -> None:
        while True:
            transition, future = self.queue.get()
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(self.loop.run_until_complete(self.report(transition)))
            except BaseException as error:  # noqa: BLE001 - the future carries it
                future.set_exception(error)

    async def report(self, transition: ActionTransition) -> None:
        if not self.started:
            try:
                await run_executor_started(settings.viewsets_command_middleware)
                self.started = True
            except Exception:
                logger.exception("executor_started() failed in process %d", os.getpid())
        middlewares = effective_middlewares(resolve_action_configuration(transition.viewset_class, transition.action))
        await run_transition_listeners(middlewares, transition, TRANSITION_LISTENER_TIMEOUT)


_transition_loop: _TransitionLoop | None = None
_transition_loop_lock = threading.Lock()


def _submit_transition(transition: ActionTransition) -> concurrent.futures.Future:
    global _transition_loop
    with _transition_loop_lock:
        if _transition_loop is None or _transition_loop.pid != os.getpid():
            _transition_loop = _TransitionLoop()
        return _transition_loop.submit(transition)


def _drain_transitions(**_signal) -> None:
    """Waits up to `TRANSITION_DRAIN_TIMEOUT` seconds for the transitions this process submitted and
    has not reported yet. The listeners run on a daemon thread, which ends with the process."""
    with _transition_loop_lock:
        transition_loop = _transition_loop
        if transition_loop is None or transition_loop.pid != os.getpid():
            return
    pending = transition_loop.pending()
    if not pending:
        return
    _done, not_done = concurrent.futures.wait(pending, timeout=TRANSITION_DRAIN_TIMEOUT)
    if not_done:
        logger.warning("%d transition(s) not reported before process %d exits", len(not_done), os.getpid())


def _report(
    kind: str, task_name: str | None, task_kwargs: Any, error_of: Callable[[str], DfViewSetError]
) -> concurrent.futures.Future | None:
    """
    Pushes the error `error_of(task)` to the caller of the call `task_kwargs` belong to, then submits
    the transition. Does nothing for a task outside the registered prefixes, for kwargs without the
    call's result queue and for a call already reported in this process.
    """
    resolved = _action_of(task_name)
    if resolved is None or not isinstance(task_kwargs, dict):
        return None
    correlation_id = task_kwargs.get("_correlation_id")
    result_queue_key = task_kwargs.get("_result_queue_key")
    if not correlation_id or not result_queue_key or not _first_report(correlation_id):
        return None
    viewset_class, action, redis_client = resolved
    error = error_of(task_name)
    logger.warning("Celery task %s: %s (correlation_id=%s): %s", kind, task_name, correlation_id, error.detail)
    try:
        push_payload(redis_client, result_queue_key, error_payload(correlation_id, task_name, error))
    except Exception:
        logger.exception("Celery task %s: %s not reported to its caller", kind, task_name)
    try:
        context = Context(
            deserialize_context(task_kwargs.get(CONTEXT_KWARG) or {}),
            action_configuration=resolve_action_configuration(viewset_class, action),
            action_name=action,
        )
        parent = ExecutionRef.from_jsonable(task_kwargs.get(PARENT_KWARG))
    except Exception:
        logger.exception("Celery task %s: %s carries no readable context", kind, task_name)
        return None
    return _submit_transition(ActionTransition(kind, viewset_class, action, context, parent, error))


def _message_call(message: Any) -> tuple[str | None, dict | None]:
    """The task name and kwargs of a task message; None for what cannot be decoded."""
    try:
        name = (message.headers or {}).get("task")
        body = message.decode()
    except Exception:
        return None, None
    if isinstance(body, (list, tuple)) and len(body) == 3 and isinstance(body[1], dict):
        return name, body[1]
    if isinstance(body, dict):
        return name or body.get("task"), body.get("kwargs")
    return name, None


def _on_task_failure(sender=None, exception=None, kwargs=None, **_signal):
    """Celery's supervisor reports a hard time limit and a lost worker process here; any other
    failure the execution already pushed itself."""
    billiard = _billiard_exceptions()
    if isinstance(exception, billiard.TimeLimitExceeded):
        reason = "time_limit"
    elif isinstance(exception, billiard.WorkerLostError):
        reason = "lost"
    else:
        return None
    return _report("interrupted", getattr(sender, "name", None), kwargs, lambda t: ActionInterruptedError(t, reason))


def _on_task_revoked(sender=None, request=None, terminated=False, expired=False, **_signal):
    task_kwargs = getattr(request, "kwargs", None)
    task_name = getattr(sender, "name", None) or getattr(request, "task", None)
    if terminated:
        return _report("interrupted", task_name, task_kwargs, lambda t: ActionInterruptedError(t, "terminated"))
    reason = "expired" if expired else "revoked"
    return _report("not_run", task_name, task_kwargs, lambda t: ActionDispatchError(t, reason))


def _on_task_unknown(message=None, name=None, **_signal):
    message_name, task_kwargs = _message_call(message)
    return _report("not_run", name or message_name, task_kwargs, lambda t: ActionDispatchError(t, "unknown_task"))


def _on_task_rejected(message=None, **_signal):
    task_name, task_kwargs = _message_call(message)
    return _report("not_run", task_name, task_kwargs, lambda t: ActionDispatchError(t, "rejected"))


def _on_task_internal_error(sender=None, kwargs=None, **_signal):
    return _report("failed", getattr(sender, "name", None), kwargs, lambda t: ActionFailedError(t, "executor"))


_receivers_connected = False


def _connect_receivers() -> None:
    global _receivers_connected
    if _receivers_connected:
        return
    from celery import signals

    for name, receiver in (
        ("task_failure", _on_task_failure),
        ("task_revoked", _on_task_revoked),
        ("task_unknown", _on_task_unknown),
        ("task_rejected", _on_task_rejected),
        ("task_internal_error", _on_task_internal_error),
    ):
        getattr(signals, name).connect(receiver, weak=False, dispatch_uid=f"fastapi_viewsets.{name}")
    # a warm shutdown sends worker_shutdown, a pool child sends worker_process_shutdown and exits
    # without running atexit, and a cold shutdown sends neither
    for name in ("worker_shutdown", "worker_process_shutdown"):
        getattr(signals, name).connect(_drain_transitions, weak=False, dispatch_uid=f"fastapi_viewsets.{name}")
    atexit.register(_drain_transitions)
    _receivers_connected = True


def celery_viewset_server(
    celery_app: "Celery",
    task_prefix: str,
    lifecycle: LifecycleType = "singleton",
    redis_client: "redis.Redis | None" = None,
):
    """
    Decorator for the Celery worker side. Registers a task per route of the viewset that executes
    the action and pushes its result or error to the caller's result queue through `redis_client`
    (a synchronous `redis.Redis`, required). The action's arguments are decoded, and its result
    encoded, by its type hints (see `transport`); a result that cannot be encoded fails the call
    with `ActionFailedError` (reason `result`).

    The tasks are registered with `ignore_result=True`, `acks_late=False`,
    `reject_on_worker_lost=False` and `store_errors_even_if_ignored=False`, overriding the
    application's `task_acks_late`: a call is acknowledged when its execution starts, and never
    delivered a second time, since its caller receives one outcome only.

    No celery, billiard or kombu exception reaches `Middleware.execute()` or the caller: a soft
    time limit, and the gevent pool's hard time limit, arrive as `ActionInterruptedError`, any other
    one as `ActionFailedError`. The
    worker's supervisor reports what ends an execution from outside it (a hard time limit, a
    termination, a lost process) and a call that never runs (revoked, expired, unknown, rejected)
    to the caller and to `Middleware.on_transition()`; a process no supervisor outlives (any pool but
    `prefork`) reports its own exit as `interrupted` with reason `terminated`.
    """
    if redis_client is None:
        raise ValueError("redis_client is required for celery_viewset_server")

    def decorator(cls: type[T]):
        seen_tasks = set()
        instance = cls() if lifecycle == "singleton" else None

        def get_sync_wrapper(original_endpoint, task_name: str):
            parameters = inspect.signature(original_endpoint).parameters
            codec = ActionCodec(cls, original_endpoint)

            @wraps(original_endpoint)
            def sync_wrapper(*args, **kwargs):
                nonlocal instance

                call = dict(kwargs)
                correlation_id = kwargs.pop("_correlation_id", None)
                result_queue_key = kwargs.pop("_result_queue_key", None)
                run = _Run(task_name)

                logger.info("Celery task executing: %s (correlation_id=%s)", task_name, correlation_id)
                try:
                    loop = _event_loop()
                    _ensure_executor_started(loop, task_name)
                    context = Context(deserialize_context(kwargs.pop(CONTEXT_KWARG, None) or {}))
                    request = request_from_jsonable(kwargs.pop(REQUEST_KWARG, None))
                    parent = ExecutionRef.from_jsonable(kwargs.pop(PARENT_KWARG, None))
                    untyped = kwargs.pop(UNTYPED_KWARG, None) or ()
                    args = tuple(from_jsonable(arg) for arg in args)
                    kwargs = _reconstruct_kwargs(original_endpoint, kwargs, cls, untyped, codec)
                    marker = execution_translator.set(run.translate)
                    try:
                        with _soft_limit_raises(task_name):
                            result = _run_to_end(
                                loop,
                                lifecycle_runner(
                                    original_endpoint,
                                    instance,
                                    cls,
                                    lifecycle,
                                    *args,
                                    needs_context="context" in parameters,
                                    request=request,
                                    forwards_request="request" in parameters,
                                    context=context,
                                    parent=parent,
                                    **kwargs,
                                ),
                                run,
                            )
                    finally:
                        execution_translator.reset(marker)
                    logger.info("Celery task completed: %s (correlation_id=%s)", task_name, correlation_id)
                    if correlation_id and result_queue_key:
                        try:
                            data, encoding = codec.encode_result(result)
                            payload = {
                                "correlation_id": correlation_id,
                                "task": task_name,
                                "result": data,
                                "result_encoding": encoding,
                            }
                        except Exception as error:
                            raise ActionFailedError(task_name, "result") from error
                        push_payload(redis_client, result_queue_key, payload)
                        # The caller reads the pushed payload only, so the task returns a JSON-trivial
                        # value instead of handing Celery a raw endpoint return value - e.g. a Pydantic
                        # model - to encode on its own (task-succeeded event, ...).
                        return True
                    return result
                except BaseException as caught:
                    return _fail(caught, run, task_name, call)

            return sync_wrapper

        def _fail(caught: BaseException, run: _Run, task_name: str, call: dict):
            """Pushes the error of a failed execution and re-raises what Celery is to record: the
            translated error, or a process exit as it is. An `HTTPException` the action raised is its
            answer and is not re-raised.

            A process exit is reported as `interrupted` with reason `terminated` where no supervisor
            outlives the process (any process but a prefork pool child); a prefork pool child's
            supervisor reports it.

            The gevent pool's hard time limit is reported here as the supervisor reports another
            pool's, as `interrupted` with reason `time_limit`, and its `gevent.Timeout` is re-raised
            as it is: the pool handles the time limit only when that exception reaches it."""
            correlation_id = call.get("_correlation_id")
            result_queue_key = call.get("_result_queue_key")
            pool_timeout = _pool_timeout_in(caught)
            if pool_timeout is not None:
                _report("interrupted", task_name, call, lambda t: ActionInterruptedError(t, "time_limit"))
                raise pool_timeout
            error = run.translate(caught)
            process_exit = isinstance(error, (KeyboardInterrupt, SystemExit))
            if process_exit:
                pushed = ActionInterruptedError(task_name, "terminated")
            elif isinstance(error, (HTTPException, RequestValidationError)):
                # a request the worker found invalid (a filter value that will not convert) is pushed as
                # the `RequestValidationError` it is, and answered as a request the FastAPI process found
                # invalid
                pushed = error
            else:
                pushed = ActionFailedError(task_name)
            if isinstance(pushed, (ActionInterruptedError, ActionFailedError)):
                logger.error(
                    "Celery task failed: %s (correlation_id=%s): %s",
                    task_name,
                    correlation_id,
                    pushed.detail,
                    exc_info=caught,
                )
            else:
                status, detail = (
                    (pushed.status_code, pushed.detail) if isinstance(pushed, HTTPException) else (422, pushed.errors())
                )
                logger.info(
                    "Celery task returned standard error: %s (correlation_id=%s): %d: %s",
                    task_name,
                    correlation_id,
                    status,
                    detail,
                )
            if process_exit and not _supervised():
                _report("interrupted", task_name, call, lambda _t: pushed)
            elif correlation_id and result_queue_key:
                push_payload(redis_client, result_queue_key, error_payload(correlation_id, task_name, pushed))
            if process_exit:
                raise caught
            if isinstance(pushed, (ActionInterruptedError, ActionFailedError)):
                if pushed is caught:
                    raise caught
                raise pushed from caught
            return None

        build_schema(cls)
        for route in cls.__router.routes:
            task_name = f"{task_prefix}.{route.name or route.endpoint.__name__}"

            if task_name in seen_tasks:
                continue
            seen_tasks.add(task_name)
            _task_actions[task_name] = (cls, route.endpoint.__name__)

            # ignore_result=True - celery_viewset_client never reads Celery's own result backend, it
            # awaits the result queue pushed to above instead (see client.py). acks_late=False and
            # reject_on_worker_lost=False: a call is never delivered twice. store_errors_even_if_ignored=False:
            # the supervisor stores no failure in the unused backend either.
            celery_app.task(
                name=task_name,
                ignore_result=True,
                acks_late=False,
                reject_on_worker_lost=False,
                store_errors_even_if_ignored=False,
            )(get_sync_wrapper(route.endpoint, task_name))

        _prefixes[task_prefix] = _Prefix(cls, redis_client)
        _connect_receivers()
        cls.__celery_viewset_metadata__ = {"task_prefix": task_prefix, "lifecycle": lifecycle, "celery_app": celery_app}

        return cls

    return decorator
