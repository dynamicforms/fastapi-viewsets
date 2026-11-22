import asyncio
import contextlib
import json
import os
import time
import uuid

from collections import deque
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from fastapi_viewsets.decorators.celery_viewset import result_reader
from fastapi_viewsets.decorators.celery_viewset.result_reader import (
    error_payload,
    get_process_queue_key,
    get_result_queue_key,
    push_payload,
    push_payload_async,
    push_result,
    RESULT_QUEUE_TTL,
    start_result_reader,
    stop_result_reader,
)
from fastapi_viewsets.exceptions import (
    ActionFailedError,
    ActionInterruptedError,
    DfViewSetError,
    ResultReaderStoppedError,
)


class ResultLists:
    """In-memory Redis lists as the result reader uses them: BLPOP, and LPOP with or without a count."""

    def __init__(self):
        self.lists: dict[str, deque] = {}

    def push(self, key: str, *items: str) -> None:
        self.lists.setdefault(key, deque()).extend(item.encode() for item in items)

    async def blpop(self, keys: list[str], timeout: float):
        deadline = time.monotonic() + timeout
        while True:
            for key in keys:
                if self.lists.get(key):
                    return [key.encode(), self.lists[key].popleft()]
            if time.monotonic() >= deadline:
                return None
            await asyncio.sleep(0.005)

    async def lpop(self, key: str, count: int | None = None):
        items = self.lists.get(key) or deque()
        if count is None:
            return items.popleft() if items else None
        taken = [items.popleft() for _ in range(min(count, len(items)))]
        return taken or None


@pytest.fixture
def redis_url() -> str:
    url = os.environ.get("FASTAPI_VIEWSETS_TEST_REDIS_URL")
    if not url:
        pytest.skip("FASTAPI_VIEWSETS_TEST_REDIS_URL is not set")
    return url


@pytest_asyncio.fixture
async def async_redis(redis_url):
    import redis.asyncio

    client = redis.asyncio.Redis.from_url(redis_url)
    yield client
    await client.aclose()


@pytest.fixture
def queue_key(redis_url) -> str:
    import redis

    key = f"celery_viewset_results:reader-test-{uuid.uuid4().hex}"
    yield key
    client = redis.Redis.from_url(redis_url)
    client.delete(key, get_process_queue_key(key))
    client.close()


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
    redis_mock = MagicMock()
    push_result(redis_mock, "celery_viewset_results:items", "corr-1", result={"id": 1})

    pipe = redis_mock.pipeline.return_value
    redis_mock.pipeline.assert_called_once_with(transaction=False)
    pipe.rpush.assert_called_once()
    key, payload_str = pipe.rpush.call_args[0]
    assert key == "celery_viewset_results:items"
    payload = json.loads(payload_str)
    assert payload["correlation_id"] == "corr-1"
    assert payload["result"] == {"id": 1}
    assert "error" not in payload
    pipe.expire.assert_called_once_with("celery_viewset_results:items", result_reader.RESULT_QUEUE_TTL)
    pipe.execute.assert_called_once_with()


def test_push_result_error():
    """push_result pushes JSON with error to Redis."""
    redis_mock = MagicMock()
    push_result(redis_mock, "celery_viewset_results:items", "corr-2", error=Exception("something failed"))

    redis_mock.pipeline.return_value.rpush.assert_called_once()
    key, payload_str = redis_mock.pipeline.return_value.rpush.call_args[0]
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
    redis_mock = ResultLists()
    queue_key = "celery_viewset_results:test"
    correlation_id = "loop-test-1"

    payload = json.dumps({"correlation_id": correlation_id, "result": [1, 2, 3]})
    # First call returns payload, subsequent calls return None (stop polling)
    redis_mock.push(get_process_queue_key(queue_key), payload)

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
async def test_result_reader_loop_reconstructs_tagged_serializable_object():
    """A worker-side ViewSetResult (see fastapi_viewsets.middleware) crosses the Redis boundary
    tagged by _to_jsonable (server.py) - the loop must undo that tagging (deserialize_value) so the
    FastAPI-process caller receives a real ViewSetResult, not the raw tagged dict."""
    from fastapi_viewsets.middleware import ViewSetResult

    redis_mock = ResultLists()
    queue_key = "celery_viewset_results:test"
    correlation_id = "loop-test-viewset-result"

    tagged = ViewSetResult(body={"id": 1}, status_code=302, headers={"Location": "/x"}).__serialize__()
    payload = json.dumps(
        {
            "correlation_id": correlation_id,
            "result": {"__fpv_type__": "fastapi_viewsets.middleware.ViewSetResult", "__fpv_value__": tagged},
        }
    )
    redis_mock.push(get_process_queue_key(queue_key), payload)

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
    redis_mock = ResultLists()
    queue_key = "celery_viewset_results:test"
    correlation_id = "loop-test-err"

    payload = json.dumps({"correlation_id": correlation_id, "error": "task failed"})
    redis_mock.push(get_process_queue_key(queue_key), payload)

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
    from fastapi import HTTPException

    redis_mock = ResultLists()
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
    redis_mock.push(get_process_queue_key(queue_key), payload)

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
    redis_mock = ResultLists()
    queue_key = "celery_viewset_results:test"

    payload = json.dumps({"correlation_id": "unknown-id", "result": "data"})
    redis_mock.push(get_process_queue_key(queue_key), payload)

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
    redis_mock = ResultLists()

    task = await start_result_reader(redis_mock, "celery_viewset_results:test", poll_interval=0.01)
    assert task is not None
    assert not task.done()

    await stop_result_reader()
    assert task.done()


@pytest.mark.asyncio
async def test_start_result_reader_returns_existing_task():
    """start_result_reader returns existing task if already running."""
    redis_mock2 = ResultLists()

    task1 = await start_result_reader(redis_mock2, "celery_viewset_results:test", poll_interval=0.01)
    task2 = await start_result_reader(redis_mock2, "celery_viewset_results:test", poll_interval=0.01)
    assert task1 is task2

    await stop_result_reader()


@pytest.mark.asyncio
async def test_start_result_reader_supports_multiple_queue_keys():
    """A different queue_key (e.g. a different celery_viewset task_prefix) gets its own reader task."""
    redis_mock = ResultLists()

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
    redis_mock = ResultLists()

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
    redis_mock = ResultLists()
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
    redis_mock = ResultLists()
    redis_mock.push(get_process_queue_key("celery_viewset_results:t"), json.dumps(data))
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


# ---------------------------------------------------------------------------
# result reader and push on Redis (FASTAPI_VIEWSETS_TEST_REDIS_URL)
# ---------------------------------------------------------------------------


class _CommandLog:
    """Proxies a `redis.asyncio` client and records the reader's commands; `lpop_count` False rejects
    `LPOP key count` as a server before Redis 6.2 does."""

    def __init__(self, client, lpop_count: bool = True):
        self.client = client
        self.lpop_count = lpop_count
        self.commands: list[tuple] = []

    async def blpop(self, keys, timeout):
        self.commands.append(("blpop",))
        return await self.client.blpop(keys, timeout=timeout)

    async def lpop(self, key, count=None):
        from redis.exceptions import ResponseError

        self.commands.append(("lpop", count))
        if count is not None and not self.lpop_count:
            raise ResponseError("wrong number of arguments for 'lpop' command")
        return await self.client.lpop(key, count) if count is not None else await self.client.lpop(key)


def _register(count: int, queue_key: str) -> tuple[list[str], list[asyncio.Future], list[str]]:
    """`count` registered futures and the list their resolutions are appended to."""
    loop = asyncio.get_running_loop()
    ids = [f"order-{index}" for index in range(count)]
    futures, resolved = [], []
    for correlation_id in ids:
        future = loop.create_future()
        future.add_done_callback(lambda _f, correlation_id=correlation_id: resolved.append(correlation_id))
        result_reader.register_future(correlation_id, future, queue_key)
        futures.append(future)
    return ids, futures, resolved


@pytest.mark.asyncio
async def test_the_reader_drains_waiting_results_in_list_order_with_lpop_count(async_redis, queue_key):
    ids, futures, resolved = _register(250, queue_key)
    await async_redis.rpush(
        get_process_queue_key(queue_key), *(json.dumps({"correlation_id": i, "result": i}) for i in ids)
    )
    client = _CommandLog(async_redis)

    await start_result_reader(client, queue_key)
    try:
        results = await asyncio.wait_for(asyncio.gather(*futures), timeout=5)
    finally:
        await stop_result_reader(queue_key)

    assert results == ids
    assert resolved == ids
    assert client.commands[:4] == [("blpop",), ("lpop", 100), ("lpop", 100), ("lpop", 100)]


@pytest.mark.asyncio
async def test_the_reader_drains_one_payload_at_a_time_when_lpop_takes_no_count(async_redis, queue_key):
    ids, futures, resolved = _register(5, queue_key)
    await async_redis.rpush(
        get_process_queue_key(queue_key), *(json.dumps({"correlation_id": i, "result": i}) for i in ids)
    )
    client = _CommandLog(async_redis, lpop_count=False)

    await start_result_reader(client, queue_key)
    try:
        assert await asyncio.wait_for(asyncio.gather(*futures), timeout=5) == ids
    finally:
        await stop_result_reader(queue_key)

    assert resolved == ids
    assert ("lpop", None) in client.commands


@pytest.mark.asyncio
async def test_a_result_pushed_while_the_reader_waits_resolves_without_polling_delay(redis_url, async_redis, queue_key):
    import redis

    ids, [future], _resolved = _register(1, queue_key)
    await start_result_reader(async_redis, queue_key)
    try:
        await asyncio.sleep(0.2)  # the reader is blocked in BLPOP
        pusher = redis.Redis.from_url(redis_url)
        started = time.monotonic()
        await asyncio.to_thread(push_result, pusher, get_process_queue_key(queue_key), ids[0], result="ok")
        assert await asyncio.wait_for(future, timeout=1) == "ok"
        assert time.monotonic() - started < 0.5
        pusher.close()
    finally:
        await stop_result_reader(queue_key)


@pytest.mark.asyncio
async def test_a_reader_blocked_in_blpop_stops_at_once_and_fails_its_pending_calls(async_redis, queue_key):
    _ids, [future], _resolved = _register(1, queue_key)
    await start_result_reader(async_redis, queue_key)
    await asyncio.sleep(0.2)

    started = time.monotonic()
    await stop_result_reader(queue_key)

    assert time.monotonic() - started < 0.5
    with pytest.raises(ResultReaderStoppedError):
        future.result()
    assert await async_redis.ping()  # the client stays usable


class _CancelLosingClient:
    """A client whose BLPOP answers the first cancel with an empty result, as redis-py can on Python < 3.12."""

    def __init__(self):
        self.lost_cancels = 0

    async def blpop(self, _keys, timeout):
        try:
            await asyncio.sleep(timeout)
        except asyncio.CancelledError:
            if self.lost_cancels:
                raise
            self.lost_cancels += 1
        return None


@pytest.mark.asyncio
async def test_a_reader_that_loses_a_cancel_still_stops_and_fails_its_pending_calls():
    queue_key = f"reader-test-{uuid.uuid4().hex}"
    _ids, [future], _resolved = _register(1, queue_key)
    client = _CancelLosingClient()
    await start_result_reader(client, queue_key)
    await asyncio.sleep(0.05)

    started = time.monotonic()
    await asyncio.wait_for(stop_result_reader(queue_key), timeout=2)

    assert time.monotonic() - started < 0.5
    assert client.lost_cancels == 1
    with pytest.raises(ResultReaderStoppedError):
        future.result()


@pytest.mark.asyncio
async def test_a_reader_blocked_in_blpop_leaves_the_clients_other_commands_answered(async_redis, queue_key):
    await start_result_reader(async_redis, queue_key)
    try:
        await asyncio.sleep(0.2)
        started = time.monotonic()
        await asyncio.wait_for(async_redis.set(f"{queue_key}:probe", "1", ex=10), timeout=1)
        assert await asyncio.wait_for(async_redis.get(f"{queue_key}:probe"), timeout=1) == b"1"
        assert time.monotonic() - started < 0.5
    finally:
        await stop_result_reader(queue_key)
        await async_redis.delete(f"{queue_key}:probe")


def test_push_payload_appends_and_renews_the_expiry(redis_url, queue_key):
    import redis

    client = redis.Redis.from_url(redis_url)
    try:
        push_payload(client, queue_key, {"correlation_id": "a"})
        client.expire(queue_key, 10)
        push_payload(client, queue_key, {"correlation_id": "b"})
        assert [json.loads(item)["correlation_id"] for item in client.lrange(queue_key, 0, -1)] == ["a", "b"]
        assert RESULT_QUEUE_TTL - 5 < client.ttl(queue_key) <= RESULT_QUEUE_TTL
    finally:
        client.close()


@pytest.mark.asyncio
async def test_push_payload_async_appends_and_renews_the_expiry(async_redis, queue_key):
    await push_payload_async(async_redis, queue_key, {"correlation_id": "a"})
    await async_redis.expire(queue_key, 10)
    await push_payload_async(async_redis, queue_key, {"correlation_id": "b"})
    assert [json.loads(item)["correlation_id"] for item in await async_redis.lrange(queue_key, 0, -1)] == ["a", "b"]
    assert RESULT_QUEUE_TTL - 5 < await async_redis.ttl(queue_key) <= RESULT_QUEUE_TTL
