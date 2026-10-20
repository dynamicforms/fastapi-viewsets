import asyncio
import json
import logging
import os
import socket

from typing import TYPE_CHECKING

from fastapi import HTTPException
from fastapi.exceptions import RequestValidationError

from ...context import from_jsonable
from ...exceptions import _error_from_jsonable, _error_to_jsonable, ActionFailedError, ResultReaderStoppedError

if TYPE_CHECKING:
    import redis

logger = logging.getLogger(__name__)

# Seconds a result list outlives its last push. A list whose process is gone is never read again;
# the expiry removes it from Redis.
RESULT_QUEUE_TTL = 3600

# Global registry: correlation_id -> (asyncio.Future, queue_key of the reader that resolves it)
_pending_futures: dict[str, tuple[asyncio.Future, str | None]] = {}

# Global reader task registry, keyed by queue_key - one reader per Celery viewset task_prefix
_reader_tasks: dict[str, asyncio.Task] = {}


def register_future(correlation_id: str, future: asyncio.Future, queue_key: str | None = None) -> None:
    """
    Register a call's Future. When the reader of queue_key exits, a Future still pending for it
    fails with ResultReaderStoppedError.
    """
    _pending_futures[correlation_id] = (future, queue_key)


def unregister_future(correlation_id: str) -> None:
    _pending_futures.pop(correlation_id, None)


def get_result_queue_key(task_prefix: str) -> str:
    return f"celery_viewset_results:{task_prefix}"


def get_process_queue_key(queue_key: str) -> str:
    """
    The Redis list this process receives queue_key's results on.

    A pending Future exists only in the process that dispatched the call, so each process reads its
    own list, named by host and process id. The key is computed at call time: a process forked after
    import gets its own.
    """
    return f"{queue_key}:{socket.gethostname()}:{os.getpid()}"


async def result_reader_loop(redis_client: "redis.Redis", queue_key: str, poll_interval: float = 0.05) -> None:
    """
    Background coroutine that reads this process's results for queue_key and resolves pending Futures.

    On exit it fails every Future still pending for queue_key with ResultReaderStoppedError: no other
    reader pops their results.
    """
    try:
        await _read_results(redis_client, queue_key, poll_interval)
    finally:
        _fail_pending_futures(queue_key)


def _fail_pending_futures(queue_key: str) -> None:
    for correlation_id, (future, future_queue_key) in list(_pending_futures.items()):
        if future_queue_key != queue_key:
            continue
        del _pending_futures[correlation_id]
        if not future.done():
            future.set_exception(ResultReaderStoppedError(queue_key))


async def _read_results(redis_client: "redis.Redis", queue_key: str, poll_interval: float) -> None:
    process_queue_key = get_process_queue_key(queue_key)
    while True:
        try:
            # Non-blocking pop from Redis list
            item = await redis_client.lpop(process_queue_key)
            if item is None:
                await asyncio.sleep(poll_interval)
                continue

            data = json.loads(item)
            correlation_id = data.get("correlation_id")

            pending = _pending_futures.pop(correlation_id, None)
            if pending is None:
                logger.warning("No pending future for correlation_id=%s", correlation_id)
                continue
            _resolve(pending[0], data)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Error in result_reader_loop: %s", e)
            await asyncio.sleep(poll_interval)


def _resolve(future: asyncio.Future, data: dict) -> None:
    """Resolves `future` with the result or the error of one payload."""
    if future.done():
        return
    task = data.get("task")
    try:
        if data.get("error") is not None:
            future.set_exception(_payload_error(data))
        else:
            # Undoes _to_jsonable's SerializableObject tagging (see server.py) at any depth - an
            # untagged value passes through unchanged.
            future.set_result(from_jsonable(data.get("result")))
    except Exception as error:
        logger.exception("Result of %s (correlation_id=%s) cannot be read", task, data.get("correlation_id"))
        failure = ActionFailedError(task, "result")
        failure.__cause__ = error
        future.set_exception(failure)


def _payload_error(data: dict) -> Exception:
    """The exception a payload's `error` carries. An `error` that is not encoded fields is an
    `HTTPException` when the payload has `http_status_code`, else an `ActionFailedError`."""
    error = data["error"]
    if isinstance(error, dict):
        return _error_from_jsonable(error)
    if data.get("http_status_code") is not None:
        return HTTPException(status_code=data["http_status_code"], detail=data.get("http_detail"))
    return ActionFailedError(data.get("task"))


def error_payload(correlation_id: str, task: str | None, error: BaseException) -> dict:
    """
    The payload that fails one call with `error`: an `HTTPException` (a `DfViewSetError` with its
    class), a `RequestValidationError` as a 422 with its errors as `detail` and `class`
    `"RequestValidationError"`, any other error as `ActionFailedError`. `http_status_code` and
    `http_detail` repeat the status and detail for a reader that does not decode `error`.
    """
    if not isinstance(error, (HTTPException, RequestValidationError)):
        error = ActionFailedError(task)
    encoded = _error_to_jsonable(error)
    return {
        "correlation_id": correlation_id,
        "task": task,
        "error": encoded,
        "http_status_code": encoded["status_code"],
        "http_detail": encoded["detail"],
    }


def push_payload(redis_client: "redis.Redis", queue_key: str, data: dict) -> None:
    """Push one result payload to queue_key and renew the list's expiry to RESULT_QUEUE_TTL."""
    redis_client.rpush(queue_key, json.dumps(data))
    redis_client.expire(queue_key, RESULT_QUEUE_TTL)


def push_result(
    redis_client: "redis.Redis",
    queue_key: str,
    correlation_id: str,
    result=None,
    error: BaseException | None = None,
    task: str | None = None,
) -> None:
    """
    Push a result or an error for one call. queue_key is the `_result_queue_key` the client sent with
    the task. An `error` that is an `HTTPException` reaches the caller as it is (a `DfViewSetError`
    of a class `fastapi_viewsets.exceptions` defines keeps its class), and so does a
    `RequestValidationError`; any other error reaches it as `ActionFailedError`. `task` names the
    call's task in that error. The FastAPI process answers each as `fastapi_viewsets.error_responses`
    answers it.
    """
    if error is not None:
        data = error_payload(correlation_id, task, error)
    else:
        data = {"correlation_id": correlation_id, "task": task, "result": result}
    push_payload(redis_client, queue_key, data)


def get_running_queue_keys() -> frozenset[str]:
    """Return the queue_keys that currently have a running (not done) reader task."""
    return frozenset(key for key, task in _reader_tasks.items() if not task.done())


async def start_result_reader(redis_client: "redis.Redis", queue_key: str, poll_interval: float = 0.05) -> asyncio.Task:
    """
    Start the background result reader task for the given queue_key.

    Should be called once per queue_key (i.e. once per celery_viewset task_prefix) on FastAPI
    startup. Calling it again with the same queue_key while its reader is still running returns
    the existing task; a different queue_key gets its own independent reader task.
    """
    existing = _reader_tasks.get(queue_key)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(result_reader_loop(redis_client, queue_key, poll_interval))
    _reader_tasks[queue_key] = task
    return task


async def stop_result_reader(queue_key: str | None = None) -> None:
    """
    Stop background result reader task(s). Should be called on FastAPI shutdown.

    If queue_key is given, stop only that reader; otherwise stop all running readers. Calls still
    pending for a stopped reader's queue_key fail with ResultReaderStoppedError (503).
    """
    keys = [queue_key] if queue_key is not None else list(_reader_tasks.keys())
    for key in keys:
        task = _reader_tasks.pop(key, None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # A task cancelled before its first step never runs result_reader_loop's finally.
        _fail_pending_futures(key)
