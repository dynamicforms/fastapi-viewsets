import asyncio
import logging
import uuid

from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING, TypeVar

from pydantic import BaseModel

from ...context import Context, SerializableObject, serialize_context, to_jsonable
from ...exceptions import ResultTimeoutError
from ..build_schema import build_schema
from ..lifecycle_runner import CELERY_DISPATCH_ATTRIBUTE
from ..primary_key_model_helper import PARTIAL_UPDATE_ACTIONS
from . import result_reader
from .result_reader import get_process_queue_key, get_result_queue_key

if TYPE_CHECKING:
    import redis

    from celery import Celery

T = TypeVar("T")
logger = logging.getLogger(__name__)

DEFAULT_RESULT_TIMEOUT = 3600.0

_celery_dispatch_hook: Callable[[dict], Awaitable[dict]] | None = None


def set_celery_dispatch_hook(hook: Callable[[dict], Awaitable[dict]] | None) -> None:
    """Register a callable awaited just before send_task; its return dict is merged into kwargs.

    Receives the call's current kwargs (context included, if the action declares one) so a
    registered hook can read what a context processor already put there instead of needing its own
    request-level plumbing. Returns {} when there is nothing to add.
    """
    global _celery_dispatch_hook
    _celery_dispatch_hook = hook


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
    None waits without a limit.
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


def _patch_method(
    cls: type, original_endpoint, task_name: str, celery_app, queue_key: str, result_timeout: float | None
):
    """Replace the method on cls with an async version that sends a Celery task and awaits the result."""
    method_name = original_endpoint.__name__

    @wraps(original_endpoint)
    async def async_client_wrapper(_self, *args, **kwargs):
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        result_reader.register_future(correlation_id, future, queue_key)

        try:
            serializable_args = [await _serialize_value(v) for v in args]
            serializable_kwargs = {}
            for k, v in kwargs.items():
                if k == "context":
                    serializable_kwargs[k] = await serialize_context(v.raw())
                else:
                    serializable_kwargs[k] = await _serialize_value(v, method_name in PARTIAL_UPDATE_ACTIONS)
            logger.info("Celery task scheduling: %s (correlation_id=%s)", task_name, correlation_id)
            extra_kwargs = await _celery_dispatch_hook(kwargs) if _celery_dispatch_hook is not None else {}
            # send_task() blocks while it connects and publishes to the broker.
            await asyncio.to_thread(
                celery_app.send_task,
                task_name,
                args=serializable_args,
                kwargs={
                    **serializable_kwargs,
                    "_correlation_id": correlation_id,
                    "_result_queue_key": get_process_queue_key(queue_key),
                    **extra_kwargs,
                },
                # send_task() defaults ignore_result to False regardless of what the task was
                # registered with (celery_viewset_server sets ignore_result=True) - Celery's
                # per-request value overrides the task's own, so it must be repeated here or the
                # worker still tries to JSON-encode the raw return value into its result backend.
                ignore_result=True,
            )
            try:
                return await asyncio.wait_for(future, result_timeout)
            except asyncio.TimeoutError:
                logger.error("Celery task timed out: %s (correlation_id=%s)", task_name, correlation_id)
                raise ResultTimeoutError(task_name, result_timeout) from None
        finally:
            result_reader.unregister_future(correlation_id)

    setattr(async_client_wrapper, CELERY_DISPATCH_ATTRIBUTE, True)
    setattr(cls, method_name, async_client_wrapper)
