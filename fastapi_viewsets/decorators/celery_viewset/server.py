import asyncio
import inspect
import json
import logging
import types

from collections.abc import Callable
from functools import wraps
from typing import get_args, get_origin, get_type_hints, TYPE_CHECKING, TypeVar, Union

from fastapi import HTTPException
from pydantic import BaseModel

from ...context import Context, deserialize_context
from ..build_schema import build_schema
from ..lifecycle_runner import lifecycle_runner, LifecycleType
from ..route_viewset import build_type_map, resolve_typevars

if TYPE_CHECKING:
    import redis

    from celery import Celery

logger = logging.getLogger(__name__)

T = TypeVar("T")

_celery_kwargs_hook: Callable[[Callable, dict], tuple[Callable, dict]] | None = None


def set_celery_kwargs_hook(hook: Callable[[Callable, dict], tuple[Callable, dict]] | None) -> None:
    """Register a callable that may wrap the runner and/or consume kwargs before reconstruction.

    Receives (runner, kwargs), must return (runner, kwargs). Called with kwargs still raw - before
    _reconstruct_kwargs, which passes through any key it has no type hint for, so a key the hook
    does not consume would otherwise reach the action as an argument it never declared.
    """
    global _celery_kwargs_hook
    _celery_kwargs_hook = hook


def _unwrap_optional(hint):
    """Before Python 3.11, `get_type_hints()` implicitly wraps a None-defaulted parameter's
    annotation in `Optional[...]`, so a hint that is actually a bare model class arrives as
    `Union[Model, None]` and the `inspect.isclass` check below sees a `Union`, not a class."""
    if get_origin(hint) in (Union, types.UnionType):
        args = [arg for arg in get_args(hint) if arg is not type(None)]
        if len(args) == 1:
            return args[0]
    return hint


def _to_jsonable(value):
    """Recursively convert Pydantic models and lists to JSON-serializable structures."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_to_jsonable(v) for v in value]
    return value


def _reconstruct_kwargs(original_endpoint, kwargs: dict, cls: type = None) -> dict:
    """Reconstruct dict values back into Pydantic BaseModel instances based on endpoint type hints.

    `context` is handled unconditionally (by name, not by type hint) via deserialize_context - see
    fastapi_viewsets/context.py - since its declared type is a plain Context and its actual values
    may need SerializableObject-aware reconstruction.
    """
    result = {}
    remaining = {}
    for key, value in kwargs.items():
        if key == "context" and isinstance(value, dict):
            result[key] = Context(deserialize_context(value))
        else:
            remaining[key] = value

    try:
        hints = get_type_hints(original_endpoint)
    except Exception:
        result.update(remaining)
        return result

    type_map = build_type_map(cls) if cls is not None else {}

    for key, value in remaining.items():
        hint = hints.get(key)
        if hint is not None:
            hint = _unwrap_optional(resolve_typevars(type_map, hint))
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
                    run = loop.run_until_complete
                    if _celery_kwargs_hook is not None:
                        run, kwargs = _celery_kwargs_hook(run, kwargs)
                    kwargs = _reconstruct_kwargs(original_endpoint, kwargs, cls)
                    result = run(lifecycle_runner(original_endpoint, instance, cls, lifecycle, *args, **kwargs))
                    logger.info("Celery task completed: %s (correlation_id=%s)", task_name, correlation_id)
                    if correlation_id and result_queue_key and redis_client is not None:
                        redis_client.rpush(
                            result_queue_key,
                            json.dumps(
                                {
                                    "correlation_id": correlation_id,
                                    "result": _to_jsonable(result),
                                }
                            ),
                        )
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
