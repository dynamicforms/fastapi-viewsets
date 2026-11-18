"""The async implementation of a `celery_viewset` task, as an async process of Celery with async tasks
runs it: outcomes pushed to the caller when the executor cancels the execution, and
`Middleware.executor_started()` once per loop."""

import asyncio
import json
import os
import threading
import uuid

from unittest.mock import MagicMock

import pytest

from fastapi import APIRouter

from ...conf import settings
from ...middleware import Middleware
from . import server
from .server import celery_viewset_server


def async_task(name: str, monkeypatch, reason: dict, redis_client=None):
    monkeypatch.setattr(server, "_celery_cancel_reason", lambda: reason.get("value"))

    class Jobs:
        __router = APIRouter()

        @__router.post("/slow")
        async def slow(self, seconds: float) -> str:
            await asyncio.sleep(seconds)
            return "done"

        @__router.post("/broken")
        async def broken(self) -> str:
            raise ValueError("broken")

    tasks = {}
    celery_app = MagicMock()
    celery_app.task.side_effect = lambda name, **_options: lambda fun: tasks.setdefault(name, fun)
    celery_viewset_server(celery_app, "jobs", redis_client=redis_client or MagicMock())(Jobs)
    return tasks[f"jobs.{name}"].__celery_async__


def pushed(redis_client) -> list[dict]:
    """The payloads pushed through a mock client without connection settings (pushed from a thread)."""
    return [json.loads(call.args[1]) for call in redis_client.pipeline.return_value.rpush.call_args_list]


async def cancel_while_running(coroutine, reason: dict, value: str):
    task = asyncio.ensure_future(coroutine)
    await asyncio.sleep(0.1)
    reason["value"] = value
    task.cancel()
    try:
        await task
    except BaseException as error:  # noqa: BLE001 - whatever the cancellation ends in
        return error
    raise AssertionError("the execution was not interrupted")


@pytest.mark.parametrize("value", ["hard", "terminate"])
def test_an_execution_the_executor_ends_pushes_no_outcome(monkeypatch, value):
    reason, redis_client = {}, MagicMock()
    wrapper = async_task("slow", monkeypatch, reason, redis_client)
    asyncio.run(cancel_while_running(wrapper(5, _correlation_id="c1", _result_queue_key="q"), reason, value))
    assert pushed(redis_client) == []


def test_a_soft_time_limit_pushes_one_time_limit_error(monkeypatch):
    reason, redis_client = {}, MagicMock()
    wrapper = async_task("slow", monkeypatch, reason, redis_client)
    asyncio.run(cancel_while_running(wrapper(5, _correlation_id="c1", _result_queue_key="q"), reason, "soft"))
    [payload] = pushed(redis_client)
    assert payload["correlation_id"] == "c1"
    assert payload["error"]["params"]["reason"] == "time_limit"


def test_a_cancellation_while_the_result_is_pushed_delivers_the_result_only(monkeypatch):
    reason, redis_client = {}, MagicMock()
    entered, release = threading.Event(), threading.Event()

    def slow_push(*_args):
        entered.set()
        release.wait(5)

    redis_client.pipeline.return_value.execute.side_effect = slow_push
    wrapper = async_task("slow", monkeypatch, reason, redis_client)

    async def scenario():
        task = asyncio.ensure_future(wrapper(0, _correlation_id="c1", _result_queue_key="q"))
        await asyncio.to_thread(entered.wait, 5)
        reason["value"] = "soft"
        task.cancel()
        await asyncio.sleep(0.1)
        release.set()
        return await task

    assert asyncio.run(scenario()) is True
    assert [payload["result"] for payload in pushed(redis_client)] == ["done"]


def test_executor_started_is_retried_after_a_failure_no_execution_waited_for(monkeypatch):
    calls = []

    class Starting(Middleware):
        async def __call__(self, _request, _viewset, _context, call_next):
            return await call_next()

        async def executor_started(self):
            calls.append(len(calls))
            await asyncio.sleep(0.1)
            if len(calls) == 1:
                raise RuntimeError("not yet")

    monkeypatch.setattr(settings, "viewsets_command_middleware", [Starting()])
    monkeypatch.setattr(server, "_started_pid", None)

    async def scenario():
        waiter = asyncio.ensure_future(server._ensure_executor_started_on_running_loop("jobs.slow"))
        await asyncio.sleep(0.01)
        waiter.cancel()
        await asyncio.sleep(0.2)
        await server._ensure_executor_started_on_running_loop("jobs.slow")

    asyncio.run(scenario())
    assert calls == [0, 1]


# ---------------------------------------------------------------------------
# pushes from the loop with a redis.asyncio client (FASTAPI_VIEWSETS_TEST_REDIS_URL)
# ---------------------------------------------------------------------------


@pytest.fixture
def redis_url() -> str:
    url = os.environ.get("FASTAPI_VIEWSETS_TEST_REDIS_URL")
    if not url:
        pytest.skip("FASTAPI_VIEWSETS_TEST_REDIS_URL is not set")
    return url


@pytest.fixture
def redis_client(redis_url):
    import redis

    client = redis.Redis.from_url(redis_url)
    yield client
    client.close()


@pytest.fixture
def no_threads(monkeypatch):
    """Fails a push run in a thread."""

    def to_thread(function, *_args, **_kwargs):
        raise AssertionError(f"{function} ran in a thread")

    monkeypatch.setattr(asyncio, "to_thread", to_thread)


@pytest.mark.usefixtures("no_threads")
def test_the_result_is_pushed_from_the_loop_with_an_async_client(monkeypatch, redis_client):
    from .result_reader import RESULT_QUEUE_TTL

    key = f"async-push-{uuid.uuid4().hex}"
    wrapper = async_task("slow", monkeypatch, {}, redis_client)
    try:
        assert asyncio.run(wrapper(0, _correlation_id="c1", _result_queue_key=key)) is True
        [payload] = [json.loads(item) for item in redis_client.lrange(key, 0, -1)]
        assert (payload["correlation_id"], payload["result"]) == ("c1", "done")
        assert 0 < redis_client.ttl(key) <= RESULT_QUEUE_TTL
    finally:
        redis_client.delete(key)


@pytest.mark.usefixtures("no_threads")
def test_an_error_is_pushed_from_the_loop_with_an_async_client(monkeypatch, redis_client):
    from ...exceptions import ActionFailedError

    key = f"async-push-{uuid.uuid4().hex}"
    wrapper = async_task("broken", monkeypatch, {}, redis_client)
    try:
        with pytest.raises(ActionFailedError):
            asyncio.run(wrapper(_correlation_id="c2", _result_queue_key=key))
        [payload] = [json.loads(item) for item in redis_client.lrange(key, 0, -1)]
        assert (payload["correlation_id"], payload["error"]["class"]) == ("c2", "ActionFailedError")
    finally:
        redis_client.delete(key)


def test_each_loop_pushes_with_its_own_async_client(redis_client):
    from .result_reader import push_payload_async

    per_loop = server._async_redis_of(redis_client)
    key = f"async-push-{uuid.uuid4().hex}"

    async def push(correlation_id: str):
        await push_payload_async(per_loop, key, {"correlation_id": correlation_id})
        return per_loop.client()

    try:
        first, second = asyncio.run(push("a")), asyncio.run(push("b"))
        assert first is not second
        assert [json.loads(item)["correlation_id"] for item in redis_client.lrange(key, 0, -1)] == ["a", "b"]
    finally:
        redis_client.delete(key)


def test_pushes_beyond_the_connection_limit_wait_for_a_free_connection(redis_url):
    import redis

    from .result_reader import push_payload_async

    limited = redis.Redis(connection_pool=redis.ConnectionPool.from_url(redis_url, max_connections=2))
    per_loop = server._async_redis_of(limited)
    key = f"async-push-{uuid.uuid4().hex}"

    async def push_all():
        await asyncio.gather(*(push_payload_async(per_loop, key, {"correlation_id": str(n)}) for n in range(50)))

    try:
        asyncio.run(push_all())
        assert limited.llen(key) == 50
    finally:
        limited.delete(key)
        limited.close()


@pytest.mark.parametrize(
    ("url", "connection_class", "expected"),
    [
        ("redis://user:secret@db.local:6380/3", "Connection", {"host": "db.local", "port": 6380, "db": 3}),
        ("rediss://db.local:6380/1", "SSLConnection", {"host": "db.local", "port": 6380, "db": 1}),
        ("unix:///run/redis.sock?db=2", "UnixDomainSocketConnection", {"path": "/run/redis.sock", "db": 2}),
    ],
)
def test_the_async_client_takes_the_sync_clients_connection_settings(url, connection_class, expected):
    import redis
    import redis.asyncio.connection

    async def pool():
        client = server._async_redis_of(redis.Redis.from_url(url)).client()
        try:
            return client.connection_pool
        finally:
            await client.aclose()

    connection_pool = asyncio.run(pool())
    assert connection_pool.connection_class is getattr(redis.asyncio.connection, connection_class)
    assert expected.items() <= connection_pool.connection_kwargs.items()
    if url.startswith("redis://"):
        assert (connection_pool.connection_kwargs["username"], connection_pool.connection_kwargs["password"]) == (
            "user",
            "secret",
        )


def test_a_client_without_connection_settings_has_no_async_client():
    assert server._async_redis_of(MagicMock()) is None


def test_a_tls_client_gets_an_async_client_that_connects_with_its_tls_settings():
    import redis
    import redis.asyncio.connection

    async def connection():
        client = server._async_redis_of(
            redis.Redis(host="db.local", port=6380, ssl=True, ssl_ca_certs="/ca.pem")
        ).client()
        try:
            return client.connection_pool.make_connection()
        finally:
            await client.aclose()

    made = asyncio.run(connection())
    assert isinstance(made, redis.asyncio.connection.SSLConnection)
    assert (made.host, made.port, made.ssl_context.ca_certs) == ("db.local", 6380, "/ca.pem")


def test_a_tls_client_with_ocsp_validation_has_no_async_client():
    import redis

    assert server._async_redis_of(redis.Redis(host="db.local", ssl=True, ssl_validate_ocsp=True)) is None


def test_a_sentinel_client_has_no_async_client():
    from redis.sentinel import Sentinel

    assert server._async_redis_of(Sentinel([("localhost", 26379)]).master_for("primary")) is None


def test_a_client_with_a_custom_connection_class_has_no_async_client():
    import redis

    class TracedConnection(redis.Connection):
        pass

    pool = redis.ConnectionPool(host="db.local", connection_class=TracedConnection)
    assert server._async_redis_of(redis.Redis(connection_pool=pool)) is None
