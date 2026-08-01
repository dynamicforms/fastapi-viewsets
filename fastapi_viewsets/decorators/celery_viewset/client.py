import asyncio
import logging
import uuid

from functools import wraps
from typing import TYPE_CHECKING, TypeVar

from fastapi.routing import APIRoute
from pydantic import BaseModel

from ...context import Context, serialize_context
from ..build_schema import build_schema
from . import result_reader
from .result_reader import get_result_queue_key

if TYPE_CHECKING:
    import redis

    from celery import Celery

T = TypeVar("T")
logger = logging.getLogger(__name__)

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
):
    """
    Decorator for FastAPI side. Replaces viewset methods with async wrappers that
    send Celery tasks and await results via a Redis result queue.
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

            _patch_method(cls, route.endpoint, task_name, celery_app, redis_client, queue_key)

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


async def _serialize_value(value):
    """Convert a Context or BaseModel value into a JSON-safe structure for Celery/Kombu transport."""
    if isinstance(value, Context):
        return await serialize_context(value.raw())
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


def _patch_method(cls: type, original_endpoint, task_name: str, celery_app, redis_client, queue_key: str):
    """Replace the method on cls with an async version that sends a Celery task and awaits the result."""
    method_name = original_endpoint.__name__

    @wraps(original_endpoint)
    async def async_client_wrapper(_self, *args, **kwargs):
        correlation_id = str(uuid.uuid4())
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        result_reader.register_future(correlation_id, future)

        try:
            serializable_args = [await _serialize_value(v) for v in args]
            serializable_kwargs = {}
            for k, v in kwargs.items():
                if k == "context":
                    serializable_kwargs[k] = await serialize_context(v.raw())
                else:
                    serializable_kwargs[k] = await _serialize_value(v)
            logger.info("Celery task scheduling: %s (correlation_id=%s)", task_name, correlation_id)
            celery_app.send_task(
                    task_name,
                    args=serializable_args,
                    kwargs={**serializable_kwargs, "_correlation_id": correlation_id, "_result_queue_key": queue_key},
            )
            return await future
        except Exception:
            result_reader.unregister_future(correlation_id)
            raise

    setattr(cls, method_name, async_client_wrapper)
