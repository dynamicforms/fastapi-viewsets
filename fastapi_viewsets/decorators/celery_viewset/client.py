import asyncio
import logging
import uuid

from functools import wraps
from typing import TYPE_CHECKING, TypeVar

from fastapi.routing import APIRoute
from pydantic import BaseModel

from ..build_schema import build_schema
from . import result_reader
from .result_reader import get_result_queue_key

if TYPE_CHECKING:
    import redis

    from celery import Celery

T = TypeVar("T")
logger = logging.getLogger(__name__)


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

        return cls

    return decorator


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
            serializable_kwargs = {
                k: v.model_dump() if isinstance(v, BaseModel) else v
                for k, v in kwargs.items()
            }
            logger.info("Celery task scheduling: %s (correlation_id=%s)", task_name, correlation_id)
            celery_app.send_task(
                    task_name,
                    args=args,
                    kwargs={**serializable_kwargs, "_correlation_id": correlation_id, "_result_queue_key": queue_key},
            )
            return await future
        except Exception:
            result_reader.unregister_future(correlation_id)
            raise

    setattr(cls, method_name, async_client_wrapper)
