import asyncio
import contextlib

from unittest.mock import AsyncMock, MagicMock

import pytest

from fastapi_viewsets.decorators.celery_viewset import result_reader
from fastapi_viewsets.decorators.celery_viewset.result_reader import (
    error_payload,
    get_process_queue_key,
    get_result_queue_key,
    push_result,
    start_result_reader,
    stop_result_reader,
)
from fastapi_viewsets.exceptions import ActionFailedError, ActionInterruptedError, DfViewSetError

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


def test_get_process_queue_key_names_host_and_process(monkeypatch):
    """Each process has its own result list: the key carries host name and process id."""
    monkeypatch.setattr(result_reader.socket, "gethostname", lambda: "web-1")
    monkeypatch.setattr(result_reader.os, "getpid", lambda: 101)
    assert get_process_queue_key("celery_viewset_results:items") == "celery_viewset_results:items:web-1:101"

    monkeypatch.setattr(result_reader.os, "getpid", lambda: 102)
    assert get_process_queue_key("celery_viewset_results:items") == "celery_viewset_results:items:web-1:102"


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
    redis_mock.expire.assert_called_once_with("celery_viewset_results:items", result_reader.RESULT_QUEUE_TTL)


def test_push_result_error():
    """push_result pushes JSON with error to Redis."""
    import json

    redis_mock = MagicMock()
    push_result(redis_mock, "celery_viewset_results:items", "corr-2", error=Exception("something failed"))

    redis_mock.rpush.assert_called_once()
    key, payload_str = redis_mock.rpush.call_args[0]
    payload = json.loads(payload_str)
    assert payload["correlation_id"] == "corr-2"
    assert payload["error"]["class"] == "ActionFailedError"
    assert payload["error"]["detail"] == "The action failed"
    assert payload["http_status_code"] == 500
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
    redis_mock.lpop.assert_called_with(get_process_queue_key(queue_key))


@pytest.mark.asyncio
async def test_result_reader_loop_reconstructs_tagged_serializable_object():
    """A worker-side ViewSetResult (see fastapi_viewsets.middleware) crosses the Redis boundary
    tagged by _to_jsonable (server.py) - the loop must undo that tagging (deserialize_value) so the
    FastAPI-process caller receives a real ViewSetResult, not the raw tagged dict."""
    import json

    from fastapi_viewsets.middleware import ViewSetResult

    redis_mock = AsyncMock()
    queue_key = "celery_viewset_results:test"
    correlation_id = "loop-test-viewset-result"

    tagged = ViewSetResult(body={"id": 1}, status_code=302, headers={"Location": "/x"}).__serialize__()
    payload = json.dumps(
        {
            "correlation_id": correlation_id,
            "result": {"__fpv_type__": "fastapi_viewsets.middleware.ViewSetResult", "__fpv_value__": tagged},
        }
    )
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

    assert isinstance(result, ViewSetResult)
    assert result.body == {"id": 1}
    assert result.status_code == 302
    assert result.headers == {"Location": "/x"}


@pytest.mark.asyncio
async def test_result_reader_loop_reads_a_legacy_error_as_action_failed():
    """An `error` that is a string, without `http_status_code`, fails the call with ActionFailedError."""
    import json

    redis_mock = AsyncMock()
    queue_key = "celery_viewset_results:test"
    correlation_id = "loop-test-err"

    payload = json.dumps({"correlation_id": correlation_id, "error": "task failed"})
    redis_mock.lpop.side_effect = [payload.encode(), None, None]

    future = asyncio.get_event_loop().create_future()
    result_reader.register_future(correlation_id, future)

    task = asyncio.create_task(result_reader.result_reader_loop(redis_mock, queue_key, poll_interval=0.01))
    with pytest.raises(ActionFailedError):
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

    payload = json.dumps(
        {
            "correlation_id": correlation_id,
            "error": "404: Item with pk 5 not found",
            "http_status_code": 404,
            "http_detail": "Item with pk 5 not found",
        }
    )
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


@pytest.mark.asyncio
async def test_start_result_reader_supports_multiple_queue_keys():
    """A different queue_key (e.g. a different celery_viewset task_prefix) gets its own reader task."""
    redis_mock = AsyncMock()
    redis_mock.lpop.return_value = None

    task_items = await start_result_reader(redis_mock, "celery_viewset_results:items", poll_interval=0.01)
    task_orders = await start_result_reader(redis_mock, "celery_viewset_results:orders", poll_interval=0.01)

    assert task_items is not task_orders
    assert not task_items.done()
    assert not task_orders.done()

    await stop_result_reader()
    assert task_items.done()
    assert task_orders.done()


@pytest.mark.asyncio
async def test_stop_result_reader_with_queue_key_stops_only_that_reader():
    """stop_result_reader(queue_key) cancels only the reader for that queue_key."""
    redis_mock = AsyncMock()
    redis_mock.lpop.return_value = None

    task_items = await start_result_reader(redis_mock, "celery_viewset_results:items", poll_interval=0.01)
    task_orders = await start_result_reader(redis_mock, "celery_viewset_results:orders", poll_interval=0.01)

    await stop_result_reader("celery_viewset_results:items")
    assert task_items.done()
    assert not task_orders.done()

    await stop_result_reader()
    assert task_orders.done()


@pytest.mark.asyncio
async def test_stopped_reader_fails_only_its_own_pending_futures():
    """Stopping a queue_key's reader fails the futures registered for it and leaves other keys' futures pending."""
    from fastapi_viewsets.exceptions import ResultReaderStoppedError

    redis_mock = MagicMock()
    redis_mock.lpop = AsyncMock(return_value=None)
    loop = asyncio.get_running_loop()
    own = loop.create_future()
    other = loop.create_future()
    result_reader.register_future("own-id", own, "q:own")
    result_reader.register_future("other-id", other, "q:other")

    await start_result_reader(redis_mock, "q:own")
    await stop_result_reader("q:own")

    with pytest.raises(ResultReaderStoppedError):
        own.result()
    assert not other.done()
    assert "own-id" not in result_reader._pending_futures
    assert "other-id" in result_reader._pending_futures
    result_reader.unregister_future("other-id")


async def _read_one(data: dict) -> asyncio.Future:
    """A future registered for `data`'s correlation id, once the reader has read `data`."""
    import json

    redis_mock = AsyncMock()
    redis_mock.lpop.side_effect = [json.dumps(data).encode(), None, None]
    future = asyncio.get_event_loop().create_future()
    result_reader.register_future(data["correlation_id"], future)
    task = asyncio.create_task(result_reader.result_reader_loop(redis_mock, "celery_viewset_results:t", 0.01))
    try:
        await asyncio.wait({future}, timeout=2.0)
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
    return future


@pytest.mark.asyncio
async def test_an_encoded_error_is_raised_as_its_class_with_code_params_and_headers():
    error = ActionInterruptedError("items.list_items", "terminated")
    error.headers = {"Retry-After": "5"}
    payload = error_payload("enc-1", "items.list_items", error)
    future = await _read_one(payload)
    raised = future.exception()
    assert type(raised) is ActionInterruptedError
    assert (raised.status_code, raised.code) == (503, "fastapi-viewsets.action_interrupted")
    assert raised.params == {"task": "items.list_items", "reason": "terminated"}
    assert raised.headers == {"Retry-After": "5"}


@pytest.mark.asyncio
async def test_an_error_of_an_unknown_class_is_raised_as_df_viewset_error():
    encoded = {
        "status_code": 409,
        "detail": "taken",
        "headers": None,
        "class": "AppError",
        "code": "taken",
        "params": {},
    }
    future = await _read_one({"correlation_id": "enc-2", "error": encoded})
    raised = future.exception()
    assert type(raised) is DfViewSetError
    assert (raised.status_code, raised.code) == (409, "taken")


@pytest.mark.asyncio
async def test_a_result_that_cannot_be_read_fails_the_call():
    result = {"__fpv_type__": "no.such.Class", "__fpv_value__": {}}
    future = await _read_one({"correlation_id": "enc-3", "task": "items.list_items", "result": result})
    raised = future.exception()
    assert isinstance(raised, ActionFailedError)
    assert raised.params == {"task": "items.list_items", "reason": "result"}
