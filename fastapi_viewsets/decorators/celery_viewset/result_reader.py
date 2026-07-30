import asyncio
import json
import logging

from typing import TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    import redis

logger = logging.getLogger(__name__)

# Global registry: correlation_id -> asyncio.Future
_pending_futures: dict[str, asyncio.Future] = {}

# Global reader task registry, keyed by queue_key - one reader per Celery viewset task_prefix
_reader_tasks: dict[str, asyncio.Task] = {}


def register_future(correlation_id: str, future: asyncio.Future) -> None:
    _pending_futures[correlation_id] = future


def unregister_future(correlation_id: str) -> None:
    _pending_futures.pop(correlation_id, None)


def get_result_queue_key(task_prefix: str) -> str:
    return f"celery_viewset_results:{task_prefix}"


async def result_reader_loop(redis_client: "redis.Redis", queue_key: str, poll_interval: float = 0.05) -> None:
    """Background coroutine that reads results from Redis list and resolves pending Futures."""
    while True:
        try:
            # Non-blocking pop from Redis list
            item = await redis_client.lpop(queue_key)
            if item is None:
                await asyncio.sleep(poll_interval)
                continue

            data = json.loads(item)
            correlation_id = data.get("correlation_id")
            result = data.get("result")
            error = data.get("error")

            future = _pending_futures.pop(correlation_id, None)
            if future is None:
                logger.warning("No pending future for correlation_id=%s", correlation_id)
                continue

            if not future.done():
                if error is not None:
                    http_status_code = data.get("http_status_code")
                    http_detail = data.get("http_detail")
                    if http_status_code is not None:
                        future.set_exception(HTTPException(status_code=http_status_code, detail=http_detail))
                    else:
                        future.set_exception(Exception(error))
                else:
                    future.set_result(result)

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.exception("Error in result_reader_loop: %s", e)
            await asyncio.sleep(poll_interval)


def push_result(redis_client: "redis.Redis", queue_key: str, correlation_id: str, result=None, error=None) -> None:
    """Called from Celery worker to push result into Redis queue."""
    data = {"correlation_id": correlation_id}
    if error is not None:
        data["error"] = str(error)
    else:
        data["result"] = result
    redis_client.rpush(queue_key, json.dumps(data))


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

    If queue_key is given, stop only that reader; otherwise stop all running readers.
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
