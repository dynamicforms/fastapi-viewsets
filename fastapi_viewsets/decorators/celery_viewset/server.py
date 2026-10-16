import asyncio
import contextlib
import functools
import inspect
import logging
import types

from functools import wraps
from typing import Any, get_args, get_origin, get_type_hints, TYPE_CHECKING, TypeVar, Union

from fastapi import HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, TypeAdapter, ValidationError

from ...context import Context, deserialize_context, to_jsonable
from ...middleware import ExecutionRef
from ..build_schema import build_schema
from ..lifecycle_runner import lifecycle_runner, LifecycleType
from ..primary_key_model_helper import (
    create_model_without_pk,
    from_partial_body,
    partial_body_annotation,
    PARTIAL_UPDATE_ACTIONS,
    typecast_to_original_model,
)
from ..route_viewset import build_type_map, resolve_typevars
from .payload import CONTEXT_KWARG, PARENT_KWARG, request_from_jsonable, REQUEST_KWARG
from .result_reader import push_payload

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


# Alias, not a re-implementation: the actual conversion (Pydantic models, dicts, lists/tuples,
# SerializableObject values - e.g. a ViewSetResult a worker-side action returned directly, see
# fastapi_viewsets.middleware) lives in fastapi_viewsets.context.to_jsonable, shared with
# ViewSetResult's own (de)serialization. Kept under this name since call sites/tests here already
# import it as `_to_jsonable`.
_to_jsonable = to_jsonable


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


def _reconstruct_kwargs(original_endpoint, kwargs: dict, cls: type = None) -> dict:
    """Reconstruct dict values back into Pydantic BaseModel instances based on endpoint type hints.

    The call's context, request and parent arrive under the reserved kwargs of `payload`, which the
    task consumes before this runs.
    """
    result = {}
    remaining = dict(kwargs)

    try:
        hints = get_type_hints(original_endpoint)
    except Exception:
        result.update(remaining)
        return result

    type_map = build_type_map(cls) if cls is not None else {}

    partial = original_endpoint.__name__ in PARTIAL_UPDATE_ACTIONS
    for key, value in remaining.items():
        hint = hints.get(key)
        if hint is not None:
            hint = resolve_typevars(type_map, hint)
        partial_annotation = partial_body_annotation(hint, None) if partial else None
        if partial_annotation is not None:
            # validated as the partial model the FastAPI side validated it with, so the model holds
            # the fields the request stated and no others
            result[key] = from_partial_body(TypeAdapter(partial_annotation).validate_python(value, by_name=True), hint)
        else:
            result[key] = _reconstruct_value(hint, value)
    return result


def _run_to_end(loop: asyncio.AbstractEventLoop, coroutine) -> Any:
    """Runs `coroutine` on `loop` as a task and returns its result. An exception that interrupts the
    loop while the task is suspended (Celery's time limit, raised from a signal handler) cancels the
    task and runs it until it has unwound, so its `finally` blocks and `Middleware.execute()` see the
    interruption; the exception is then raised."""
    task = loop.create_task(coroutine)
    try:
        return loop.run_until_complete(task)
    except BaseException:
        if not task.done():
            task.cancel()
            with contextlib.suppress(BaseException):  # the interruption is what propagates
                loop.run_until_complete(task)
        raise


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
            parameters = inspect.signature(original_endpoint).parameters

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
                    context = Context(deserialize_context(kwargs.pop(CONTEXT_KWARG, None) or {}))
                    request = request_from_jsonable(kwargs.pop(REQUEST_KWARG, None))
                    parent = ExecutionRef.from_jsonable(kwargs.pop(PARENT_KWARG, None))
                    kwargs = _reconstruct_kwargs(original_endpoint, kwargs, cls)
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
                    )
                    logger.info("Celery task completed: %s (correlation_id=%s)", task_name, correlation_id)
                    if correlation_id and result_queue_key and redis_client is not None:
                        push_payload(
                            redis_client,
                            result_queue_key,
                            {"correlation_id": correlation_id, "result": _to_jsonable(result)},
                        )
                        # celery_viewset_client never reads this task's own retval (only the queue
                        # entry just pushed above), so returning `result` a second time here would
                        # hand Celery a raw endpoint return value - e.g. a Pydantic model - to
                        # encode on its own (result backend, task-succeeded event, ...), which
                        # isn't JSON-safe. ignore_result=True (set on both task registration and
                        # send_task) tells Celery to skip that, but a JSON-trivial retval here means
                        # nothing breaks even where that setting doesn't reach.
                        return True
                    return result
                except Exception as e:
                    if isinstance(e, RequestValidationError):
                        # a request the worker found invalid (a filter value that will not convert)
                        # answers as FastAPI answers one: 422 with the errors as `detail`
                        e = HTTPException(status_code=422, detail=jsonable_encoder(e.errors()))
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
                        push_payload(redis_client, result_queue_key, error_payload)
                    if not isinstance(e, HTTPException):
                        raise

            return sync_wrapper

        build_schema(cls)
        for route in cls.__router.routes:
            task_name = f"{task_prefix}.{route.name or route.endpoint.__name__}"

            if task_name in seen_tasks:
                continue
            seen_tasks.add(task_name)

            # ignore_result=True - celery_viewset_client never reads Celery's own result backend,
            # it awaits the result queue pushed to above instead (see client.py). Without this,
            # Celery still calls task.backend.mark_as_done(retval=result) on every task, and a raw
            # Pydantic model (or list of them) returned by the endpoint isn't JSON-serializable by
            # Celery's own encoder, crashing that unused store.
            celery_app.task(name=task_name, ignore_result=True)(get_sync_wrapper(route.endpoint, task_name))

        cls.__celery_viewset_metadata__ = {"task_prefix": task_prefix, "lifecycle": lifecycle, "celery_app": celery_app}

        return cls

    return decorator
