import asyncio
import inspect
import json
import logging

from functools import wraps
from typing import get_type_hints, TYPE_CHECKING, TypeVar

from fastapi import HTTPException
from pydantic import BaseModel

from ..build_schema import build_schema
from ..lifecycle_runner import lifecycle_runner, LifecycleType
from ..route_viewset import build_type_map, resolve_typevars

if TYPE_CHECKING:
    import redis

    from celery import Celery

logger = logging.getLogger(__name__)

T = TypeVar("T")


def _to_jsonable(value):
    """Recursively convert Pydantic models and lists to JSON-serializable structures."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


def _reconstruct_kwargs(original_endpoint, kwargs: dict, cls: type = None) -> dict:
    """Reconstruct dict values back into Pydantic BaseModel instances based on endpoint type hints."""
    try:
        hints = get_type_hints(original_endpoint)
    except Exception:
        return kwargs

    type_map = build_type_map(cls) if cls is not None else {}

    result = {}
    for key, value in kwargs.items():
        hint = hints.get(key)
        if hint is not None:
            hint = resolve_typevars(type_map, hint)
        if hint is not None and isinstance(value, dict) and inspect.isclass(hint) and issubclass(hint, BaseModel):
            try:
                result[key] = hint.model_validate(value)
            except Exception:
                result[key] = hint.model_construct(**value)
        else:
            result[key] = value
    return result


def celery_viewset_server(
    celery_app: "Celery",
    task_prefix: str,
    lifecycle: LifecycleType = "singleton",
    redis_client: "redis.Redis | None" = None,
):
    def decorator(cls: type[T]):
        seen_tasks = set()
        instance = cls() if lifecycle == "singleton" else None

        def get_sync_wrapper(original_endpoint, task_name: str):
            @wraps(original_endpoint)
            def sync_wrapper(*args, **kwargs):
                nonlocal instance

                # Extract pottery/result-queue params injected by client
                correlation_id = kwargs.pop("_correlation_id", None)
                result_queue_key = kwargs.pop("_result_queue_key", None)

                try:
                    loop = asyncio.get_event_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                if loop.is_running():
                    import nest_asyncio

                    nest_asyncio.apply()

                logger.info("Celery task executing: %s (correlation_id=%s)", task_name, correlation_id)
                try:
                    kwargs = _reconstruct_kwargs(original_endpoint, kwargs, cls)
                    result = loop.run_until_complete(
                        lifecycle_runner(original_endpoint, instance, cls, lifecycle, *args, **kwargs)
                    )
                    logger.info("Celery task completed: %s (correlation_id=%s)", task_name, correlation_id)
                    if correlation_id and result_queue_key and redis_client is not None:
                        redis_client.rpush(result_queue_key, json.dumps({
                            "correlation_id": correlation_id,
                            "result": _to_jsonable(result),
                        }))
                    return result
                except Exception as e:
                    if isinstance(e, HTTPException):
                        logger.info(
                            "Celery task returned standard error: %s (correlation_id=%s): %d: %s",
                            task_name,
                            correlation_id,
                            e.status_code,
                            e.detail,
                        )
                    else:
                        logger.exception("Celery task failed: %s (correlation_id=%s): %s", task_name, correlation_id, e)
                    if correlation_id and result_queue_key and redis_client is not None:
                        error_payload = {"correlation_id": correlation_id, "error": str(e)}
                        if isinstance(e, HTTPException):
                            error_payload["http_status_code"] = e.status_code
                            error_payload["http_detail"] = e.detail
                        redis_client.rpush(result_queue_key, json.dumps(error_payload))
                    if not isinstance(e, HTTPException):
                        raise

            return sync_wrapper

        build_schema(cls)
        for route in cls.__router.routes:
            task_name = f"{task_prefix}.{route.name or route.endpoint.__name__}"

            if task_name in seen_tasks:
                continue
            seen_tasks.add(task_name)

            celery_app.task(name=task_name)(get_sync_wrapper(route.endpoint, task_name))

        cls.__celery_viewset_metadata__ = {"task_prefix": task_prefix, "lifecycle": lifecycle, "celery_app": celery_app}

        return cls

    return decorator
