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

# Global reader task reference
_reader_task: asyncio.Task | None = None


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


async def start_result_reader(redis_client: "redis.Redis", queue_key: str, poll_interval: float = 0.05) -> asyncio.Task:
    """Start the background result reader task. Should be called on FastAPI startup."""
    global _reader_task
    if _reader_task is not None and not _reader_task.done():
        return _reader_task
    _reader_task = asyncio.create_task(result_reader_loop(redis_client, queue_key, poll_interval))
    return _reader_task


async def stop_result_reader() -> None:
    """Stop the background result reader task. Should be called on FastAPI shutdown."""
    global _reader_task
    if _reader_task is not None and not _reader_task.done():
        _reader_task.cancel()
        try:
            await _reader_task
        except asyncio.CancelledError:
            pass
    _reader_task = None
