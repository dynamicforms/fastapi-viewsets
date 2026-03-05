import asyncio

from unittest.mock import AsyncMock, MagicMock

import pytest

from fastapi_viewsets.decorators.celery_viewset import result_reader
from fastapi_viewsets.decorators.celery_viewset.result_reader import (
    get_result_queue_key,
    push_result,
    start_result_reader,
    stop_result_reader,
)

# ---------------------------------------------------------------------------
# register / unregister future
# ---------------------------------------------------------------------------

def test_register_and_unregister_future():
    """register_future adds future to registry, unregister_future removes it."""
    loop = asyncio.new_event_loop()
    future = loop.create_future()
    result_reader.register_future("abc-123", future)
    assert "abc-123" in result_reader._pending_futures

    result_reader.unregister_future("abc-123")
    assert "abc-123" not in result_reader._pending_futures
    loop.close()


def test_unregister_nonexistent_future_does_not_raise():
    """unregister_future on unknown id should not raise."""
    result_reader.unregister_future("nonexistent-id")  # should not raise


# ---------------------------------------------------------------------------
# get_result_queue_key
# ---------------------------------------------------------------------------

def test_get_result_queue_key():
    """get_result_queue_key returns correct Redis key."""
    assert get_result_queue_key("myapp") == "celery_viewset_results:myapp"
    assert get_result_queue_key("items") == "celery_viewset_results:items"


# ---------------------------------------------------------------------------
# push_result
# ---------------------------------------------------------------------------

def test_push_result_success():
    """push_result pushes JSON with result to Redis."""
    import json

    redis_mock = MagicMock()
    push_result(redis_mock, "celery_viewset_results:items", "corr-1", result={"id": 1})

    redis_mock.rpush.assert_called_once()
    key, payload_str = redis_mock.rpush.call_args[0]
    assert key == "celery_viewset_results:items"
    payload = json.loads(payload_str)
    assert payload["correlation_id"] == "corr-1"
    assert payload["result"] == {"id": 1}
    assert "error" not in payload


def test_push_result_error():
    """push_result pushes JSON with error to Redis."""
    import json

    redis_mock = MagicMock()
    push_result(redis_mock, "celery_viewset_results:items", "corr-2", error=Exception("something failed"))

    redis_mock.rpush.assert_called_once()
    key, payload_str = redis_mock.rpush.call_args[0]
    payload = json.loads(payload_str)
    assert payload["correlation_id"] == "corr-2"
    assert payload["error"] == "something failed"
    assert "result" not in payload


# ---------------------------------------------------------------------------
# result_reader_loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_result_reader_loop_resolves_future():
    """result_reader_loop reads from Redis and resolves the matching future."""
    import json

    redis_mock = AsyncMock()
    queue_key = "celery_viewset_results:test"
    correlation_id = "loop-test-1"

    payload = json.dumps({"correlation_id": correlation_id, "result": [1, 2, 3]})
    # First call returns payload, subsequent calls return None (stop polling)
    redis_mock.lpop.side_effect = [payload.encode(), None, None]

    future = asyncio.get_event_loop().create_future()
    result_reader.register_future(correlation_id, future)

    task = asyncio.create_task(result_reader.result_reader_loop(redis_mock, queue_key, poll_interval=0.01))
    result = await asyncio.wait_for(future, timeout=2.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert result == [1, 2, 3]


@pytest.mark.asyncio
async def test_result_reader_loop_sets_exception_on_error():
    """result_reader_loop sets exception on future when error is in payload."""
    import json

    redis_mock = AsyncMock()
    queue_key = "celery_viewset_results:test"
    correlation_id = "loop-test-err"

    payload = json.dumps({"correlation_id": correlation_id, "error": "task failed"})
    redis_mock.lpop.side_effect = [payload.encode(), None, None]

    future = asyncio.get_event_loop().create_future()
    result_reader.register_future(correlation_id, future)

    task = asyncio.create_task(result_reader.result_reader_loop(redis_mock, queue_key, poll_interval=0.01))
    with pytest.raises(Exception, match="task failed"):
        await asyncio.wait_for(future, timeout=2.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_result_reader_loop_sets_http_exception():
    """result_reader_loop reconstructs HTTPException when http_status_code is in payload."""
    import json

    from fastapi import HTTPException

    redis_mock = AsyncMock()
    queue_key = "celery_viewset_results:test"
    correlation_id = "loop-test-http-err"

    payload = json.dumps({
        "correlation_id": correlation_id,
        "error": "404: Item with pk 5 not found",
        "http_status_code": 404,
        "http_detail": "Item with pk 5 not found",
    })
    redis_mock.lpop.side_effect = [payload.encode(), None, None]

    future = asyncio.get_event_loop().create_future()
    result_reader.register_future(correlation_id, future)

    task = asyncio.create_task(result_reader.result_reader_loop(redis_mock, queue_key, poll_interval=0.01))
    with pytest.raises(HTTPException) as exc_info:
        await asyncio.wait_for(future, timeout=2.0)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Item with pk 5 not found"


@pytest.mark.asyncio
async def test_result_reader_loop_ignores_unknown_correlation_id():
    """result_reader_loop logs warning and continues when correlation_id is unknown."""
    import json

    redis_mock = AsyncMock()
    queue_key = "celery_viewset_results:test"

    payload = json.dumps({"correlation_id": "unknown-id", "result": "data"})
    redis_mock.lpop.side_effect = [payload.encode(), None]

    task = asyncio.create_task(result_reader.result_reader_loop(redis_mock, queue_key, poll_interval=0.01))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    # No exception raised - loop handled unknown id gracefully


# ---------------------------------------------------------------------------
# start / stop result_reader
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_and_stop_result_reader():
    """start_result_reader creates a background task; stop_result_reader cancels it."""
    redis_mock = AsyncMock()
    redis_mock.lpop.return_value = None

    task = await start_result_reader(redis_mock, "celery_viewset_results:test", poll_interval=0.01)
    assert task is not None
    assert not task.done()

    await stop_result_reader()
    assert task.done()


@pytest.mark.asyncio
async def test_start_result_reader_returns_existing_task():
    """start_result_reader returns existing task if already running."""
    redis_mock2 = AsyncMock()
    redis_mock2.lpop.return_value = None

    task1 = await start_result_reader(redis_mock2, "celery_viewset_results:test", poll_interval=0.01)
    task2 = await start_result_reader(redis_mock2, "celery_viewset_results:test", poll_interval=0.01)
    assert task1 is task2

    await stop_result_reader()
