import asyncio
import inspect
import logging
import uuid

from functools import wraps
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from ...action_configuration import resolve_action_configuration, TimeLimits
from ...context import _Deferred, Context, SerializableObject, serialize_context, to_jsonable
from ...exceptions import ActionDispatchError, ActionFailedError, ResultTimeoutError
from ...middleware import ActionTransition, run_transition_listeners
from ..build_schema import build_schema
from ..lifecycle_runner import current_call, DEFERRED_ACTIONS_ATTRIBUTE, effective_middlewares
from ..primary_key_model_helper import PARTIAL_UPDATE_ACTIONS
from . import result_reader
from .payload import CONTEXT_KWARG, PARENT_KWARG, REQUEST_KWARG, request_to_jsonable, UNTYPED_KWARG
from .result_reader import get_process_queue_key, get_result_queue_key
from .transport import ActionCodec, EncodedResult

if TYPE_CHECKING:
    import redis

    from celery import Celery

T = TypeVar("T")
logger = logging.getLogger(__name__)

# Seconds a `celery_viewset_client` call waits for its result when `result_timeout` is not given.
DEFAULT_RESULT_TIMEOUT = 3600.0

# queue_keys registered by celery_viewset_client so far - populated at decoration (import) time,
# used by check_result_readers() to spot a queue_key with no running result reader.
_registered_queue_keys: set[str] = set()


def get_registered_queue_keys() -> frozenset[str]:
    """Return the queue_keys registered by celery_viewset_client decorators applied so far."""
    return frozenset(_registered_queue_keys)


def celery_viewset_client(
    celery_app: "Celery",
    task_prefix: str,
    redis_client: "redis.Redis",
    result_timeout: float | None = DEFAULT_RESULT_TIMEOUT,
):
    """
    Decorator for FastAPI side. Replaces viewset methods with async wrappers that
    send Celery tasks and await results via a Redis result queue.

    A call whose result does not arrive within result_timeout seconds raises ResultTimeoutError (504).
    None waits without a limit. Arguments and results are encoded by the action's type hints (see
    `transport`).
    """
    queue_key = get_result_queue_key(task_prefix)

    def decorator(cls: type[T]):
        seen_tasks = set()

        build_schema(cls)
        for route in cls.__router.routes:
            task_name = f"{task_prefix}.{route.name or route.endpoint.__name__}"

            if task_name in seen_tasks:
                continue
            seen_tasks.add(task_name)

            _patch_method(cls, route.endpoint, task_name, celery_app, queue_key, result_timeout)

        cls.__celery_viewset_metadata__ = {
            "task_prefix": task_prefix,
            "celery_app": celery_app,
            "redis_client": redis_client,
            "queue_key": queue_key,
            "mode": "client",
        }
        _registered_queue_keys.add(queue_key)

        return cls

    return decorator


async def _serialize_value(value, partial: bool = False):
    """Convert a Context, dict, or list/tuple value into a JSON-safe structure for Celery/Kombu
    transport, recursing async since Context serialization needs to await context values. Anything
    else - a Pydantic model, a SerializableObject, or an already JSON-native value - is handled by
    the shared, synchronous fastapi_viewsets.context.to_jsonable(). With `partial`, a model is
    dumped with only the fields it has set: a partial update's body states no others."""
    if isinstance(value, Context):
        return await serialize_context(value.raw())
    if isinstance(value, dict):
        return {k: await _serialize_value(v, partial) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [await _serialize_value(v, partial) for v in value]
    if partial and isinstance(value, BaseModel) and not isinstance(value, SerializableObject):
        return value.model_dump(mode="json", exclude_unset=True)
    return to_jsonable(value)


async def _encode_arguments(
    codec: ActionCodec, positional: tuple, named: dict, partial: bool
) -> tuple[list, dict, list[str]]:
    """The Celery args and kwargs of a call's arguments, and the names of the kwargs that crossed
    untyped (see `transport`). An argument that holds an `UploadFile` is encoded in a worker thread,
    which reads the file."""

    async def encode(name: str, value):
        kind = codec.params[name].kind if name in codec.params else "untyped"
        if kind in ("model", "partial"):
            return await _serialize_value(value, partial), True
        if isinstance(value, Context):
            return await _serialize_value(value), False
        if codec.reads_uploads:
            return await asyncio.to_thread(codec.encode_argument, name, value)
        return codec.encode_argument(name, value)

    encoded_args = [await _serialize_value(value) for value in positional]
    encoded_kwargs: dict = {}
    untyped: list[str] = []
    for name, value in named.items():
        encoded_kwargs[name], typed = await encode(name, value)
        if not typed:
            untyped.append(name)
    return encoded_args, encoded_kwargs, untyped


def _time_limit_options(context: Context) -> dict:
    """The `send_task` time limit options of the call's `TimeLimits` configuration."""
    limits = context.configuration_for(TimeLimits)
    if limits is None:
        return {}
    options = {}
    if limits.soft is not None:
        options["soft_time_limit"] = limits.soft
    if limits.hard is not None:
        options["time_limit"] = limits.hard
    return options


def _patch_method(
    cls: type, original_endpoint, task_name: str, celery_app, queue_key: str, result_timeout: float | None
):
    """Replace the method on cls with an async version that sends a Celery task and awaits the result.

    The task carries the call's context, request and parent execution (see `payload`) from
    `current_call()`; a `context` the caller passes replaces the call's. Positional arguments are
    sent as the keyword arguments of their parameters, each encoded by its parameter's type hint
    (see `transport`); the names of those that crossed untyped are sent under `UNTYPED_KWARG`, and
    the result is decoded by the return annotation. The action's `TimeLimits`
    configuration is sent as the task's time limits. Arguments that cannot be serialized raise
    `ActionDispatchError` (reason `serialization`), a send that fails raises it with reason `send`,
    and a result that cannot be decoded raises `ActionFailedError` (reason `result`).
    A deferred context value (`ChangeEvents`'s scope of a call) is computed before the arguments
    are serialized; its exception propagates unchanged and nothing is sent.
    Once the task is sent, `Middleware.on_transition()` of the action's effective middlewares
    receives `dispatched`. The action's name joins the class's `DEFERRED_ACTIONS_ATTRIBUTE`."""
    method_name = original_endpoint.__name__
    declares_request = "request" in inspect.signature(original_endpoint).parameters
    codec = ActionCodec(cls, original_endpoint)
    partial = method_name in PARTIAL_UPDATE_ACTIONS

    @wraps(original_endpoint)
    async def async_client_wrapper(_self, *args, **kwargs):
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        result_reader.register_future(correlation_id, future, queue_key)

        try:
            call = current_call()
            context = kwargs.pop("context", None)
            if context is None:
                context = call.context if call is not None else Context({})
            if declares_request:
                kwargs.pop("request", None)
            parent = call.parent_of_dispatch() if call is not None else None
            action_configuration = resolve_action_configuration(cls, method_name)
            bound_context = Context(context.raw(), action_configuration=action_configuration, action_name=method_name)
            for value in list(context.raw().values()):
                if isinstance(value, _Deferred):
                    await value
            positional, named = codec.bind(args, kwargs)
            try:
                encoded_args, encoded_kwargs, untyped = await _encode_arguments(codec, positional, named, partial)
                task_kwargs = {
                    **encoded_kwargs,
                    **({UNTYPED_KWARG: untyped} if untyped else {}),
                    CONTEXT_KWARG: await serialize_context(context.raw()),
                    REQUEST_KWARG: request_to_jsonable(call.request if call is not None else None),
                    PARENT_KWARG: parent.to_jsonable() if parent is not None else None,
                    "_correlation_id": correlation_id,
                    "_result_queue_key": get_process_queue_key(queue_key),
                }
            except Exception as error:
                logger.error(
                    "Celery task arguments not serializable: %s (correlation_id=%s): %s",
                    task_name,
                    correlation_id,
                    error,
                )
                raise ActionDispatchError(task_name, "serialization") from error
            logger.info("Celery task scheduling: %s (correlation_id=%s)", task_name, correlation_id)
            try:
                # send_task() blocks while it connects and publishes to the broker.
                await asyncio.to_thread(
                    celery_app.send_task,
                    task_name,
                    args=encoded_args,
                    kwargs=task_kwargs,
                    # send_task() defaults ignore_result to False regardless of what the task was
                    # registered with (celery_viewset_server sets ignore_result=True) - Celery's
                    # per-request value overrides the task's own, so it must be repeated here or the
                    # worker still tries to JSON-encode the raw return value into its result backend.
                    ignore_result=True,
                    **_time_limit_options(bound_context),
                )
            except Exception as error:
                logger.error("Celery task not sent: %s (correlation_id=%s)", task_name, correlation_id)
                raise ActionDispatchError(task_name, "send") from error
            await run_transition_listeners(
                effective_middlewares(action_configuration),
                ActionTransition("dispatched", cls, method_name, bound_context, parent, None),
            )
            try:
                result = await asyncio.wait_for(future, result_timeout)
            except asyncio.TimeoutError:
                logger.error("Celery task timed out: %s (correlation_id=%s)", task_name, correlation_id)
                raise ResultTimeoutError(task_name, result_timeout) from None
            if not isinstance(result, EncodedResult):
                return result
            try:
                return codec.decode_result(result)
            except Exception as error:
                logger.exception("Result of %s (correlation_id=%s) cannot be read", task_name, correlation_id)
                raise ActionFailedError(task_name, "result") from error
        finally:
            result_reader.unregister_future(correlation_id)

    setattr(cls, method_name, async_client_wrapper)
    setattr(cls, DEFERRED_ACTIONS_ATTRIBUTE, frozenset(getattr(cls, DEFERRED_ACTIONS_ATTRIBUTE, ())) | {method_name})
