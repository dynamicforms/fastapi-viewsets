"""
Scenarios of `celery_viewset` dispatch and result delivery against a real Celery worker and the Redis of
FASTAPI_VIEWSETS_TEST_REDIS_URL (skipped without it): many concurrent calls, results delivered as soon as the
worker pushes them, results already waiting when a reader starts, a reader cancelled while it waits, the
task message's argument reprs, and dispatch by an app with and without `asend_task`, unreachable and in eager mode.
"""

import asyncio
import importlib
import json
import os
import subprocess
import sys
import textwrap
import time
import uuid
import warnings

import pytest

from fastapi import APIRouter, FastAPI

from ...exceptions import ActionDispatchError, ResultReaderStoppedError
from .. import route_viewset
from . import (
    celery_viewset_client,
    get_process_queue_key,
    get_result_queue_key,
    push_result,
    start_result_reader,
    stop_result_reader,
)

pytestmark = pytest.mark.celery_worker

REPOSITORY = __import__("pathlib").Path(__file__).parents[3]

WORKER_MODULE = textwrap.dedent(
    """
    import asyncio
    import os
    import time

    import redis

    from celery import Celery
    from fastapi import APIRouter

    from fastapi_viewsets.decorators import celery_viewset_server

    URL = os.environ["SCENARIO_REDIS_URL"]
    KEY = os.environ["SCENARIO_KEY"]

    app = Celery("scenario", broker=URL)
    app.conf.task_default_queue = KEY
    app.conf.broker_connection_retry_on_startup = True


    class Jobs:
        __router = APIRouter()

        @__router.post("/echo")
        async def echo(self, n: int) -> int:
            return n

        @__router.post("/stamp")
        async def stamp(self, delay: float) -> float:
            await asyncio.sleep(delay)
            return time.time()

        @__router.post("/slow")
        async def slow(self, seconds: float) -> str:
            await asyncio.sleep(seconds)
            return "done"

        @__router.post("/size")
        async def size(self, text: str) -> int:
            return len(text)


    celery_viewset_server(app, KEY, redis_client=redis.Redis.from_url(URL))(Jobs)
    """
)

POOLS = {
    "prefork": ["-P", "prefork", "-c", "4"],
    "async": ["-P", "prefork", "-c", "1", "--aconcurrency", "200"],
}


def _jobs_class():
    class Jobs:
        __router = APIRouter()

        @__router.post("/echo")
        async def echo(self, n: int) -> int: ...

        @__router.post("/stamp")
        async def stamp(self, delay: float) -> float: ...

        @__router.post("/slow")
        async def slow(self, seconds: float) -> str: ...

        @__router.post("/size")
        async def size(self, text: str) -> int: ...

    return Jobs


@pytest.fixture(scope="module")
def redis_url() -> str:
    url = os.environ.get("FASTAPI_VIEWSETS_TEST_REDIS_URL")
    if not url:
        pytest.skip("FASTAPI_VIEWSETS_TEST_REDIS_URL is not set")
    return url


@pytest.fixture(
    scope="module",
    params=[
        "prefork",
        pytest.param(
            "async",
            marks=pytest.mark.skipif(
                not hasattr(importlib.import_module("celery.app.trace"), "ASYNC_VARIANT_ATTR"),
                reason="Celery without async tasks",
            ),
        ),
    ],
)
def pool(request) -> str:
    return request.param


@pytest.fixture(scope="module")
def key(pool) -> str:
    return f"scenario-{pool}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def sync_redis(redis_url, key):
    import redis

    client = redis.Redis.from_url(redis_url)
    yield client
    for stored in client.scan_iter(match=f"*{key}*"):
        client.delete(stored)


@pytest.fixture(scope="module")
def celery_app(redis_url, key):
    from celery import Celery

    app = Celery("scenario-client", broker=redis_url)
    app.conf.task_default_queue = key
    return app


@pytest.fixture(scope="module")
def worker(tmp_path_factory, redis_url, key, celery_app, pool):
    directory = tmp_path_factory.mktemp("worker")
    (directory / "scenario_worker.py").write_text(WORKER_MODULE)
    env = {
        **os.environ,
        "SCENARIO_REDIS_URL": redis_url,
        "SCENARIO_KEY": key,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPOSITORY), str(directory), os.environ.get("PYTHONPATH")])),
    }
    command = [sys.executable, "-m", "celery", "-A", "scenario_worker", "worker", *POOLS[pool]]
    log = (directory / "worker.log").open("w")
    process = subprocess.Popen(  # noqa: S603 - the interpreter running the tests
        [*command, "--loglevel=INFO", "-n", f"{key}@%h"], cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT
    )
    try:
        deadline = time.monotonic() + 30
        while not celery_app.control.inspect(timeout=1, destination=[_node(key)]).ping():
            if process.poll() is not None or time.monotonic() > deadline:
                pytest.fail(f"the worker did not start: {(directory / 'worker.log').read_text()}")
        yield process
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
        log.close()


@pytest.fixture(
    params=[
        "send_task",
        pytest.param(
            "asend_task",
            marks=pytest.mark.skipif(
                not hasattr(importlib.import_module("celery").Celery, "asend_task"), reason="Celery without asend_task"
            ),
        ),
    ]
)
def make_app(request):
    """Makes a client Celery app that publishes with `asend_task`, or one without it (`send_task` only)."""
    from celery import Celery

    class CeleryWithoutAsendTask(Celery):
        @property
        def asend_task(self):
            raise AttributeError("asend_task")

    app_class = Celery if request.param == "asend_task" else CeleryWithoutAsendTask

    def make(name: str, broker: str, queue: str):
        app = app_class(name, broker=broker)
        app.conf.task_default_queue = queue
        return app

    return make


def _node(key: str) -> str:
    import socket

    return f"{key}@{socket.gethostname()}"


@pytest.fixture
def jobs(worker, celery_app, key):
    """Makes the client class of the worker's `Jobs` for a `redis_client`, dispatching to the running worker."""
    assert worker.poll() is None, "the worker exited"
    return lambda redis_client: _client_class(celery_app, key, redis_client)


def _client_class(celery_app, key, redis_client):
    return celery_viewset_client(celery_app, key, redis_client=redis_client, result_timeout=30)(_jobs_class())


def _async_redis(redis_url):
    import redis.asyncio

    return redis.asyncio.Redis.from_url(redis_url)


async def _stopped(queue_key: str) -> None:
    await stop_result_reader(queue_key)


# ---------------------------------------------------------------------------
# P1, P2, R1, R3: many concurrent calls
# ---------------------------------------------------------------------------


def test_many_concurrent_calls_all_answer_with_their_own_result(worker, make_app, key, redis_url):
    assert worker.poll() is None, "the worker exited"
    app = make_app("scenario-concurrent", redis_url, key)
    queue_key = get_result_queue_key(key)

    async def scenario():
        client = _async_redis(redis_url)
        await start_result_reader(client, queue_key)
        try:
            instance = _client_class(app, key, client)()
            return await asyncio.wait_for(asyncio.gather(*(instance.echo(n=n) for n in range(500))), 60)
        finally:
            await _stopped(queue_key)

    assert asyncio.run(scenario()) == list(range(500))


def test_many_concurrent_http_requests_all_answer_with_their_own_result(worker, celery_app, key, redis_url):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import asynccontextmanager

    from fastapi.testclient import TestClient

    assert worker.poll() is None, "the worker exited"
    queue_key = get_result_queue_key(key)
    reader_client = _async_redis(redis_url)

    @asynccontextmanager
    async def lifespan(_app):
        await start_result_reader(reader_client, queue_key)
        yield
        await stop_result_reader(queue_key)

    router = APIRouter()
    route_viewset(router, "/jobs")(_client_class(celery_app, key, reader_client))
    app = FastAPI(lifespan=lifespan)
    app.include_router(router)

    def post(client, n):
        response = client.post("/jobs/echo", params={"n": n})
        return response.status_code, response.json()

    with TestClient(app) as client, ThreadPoolExecutor(max_workers=50) as executor:
        answers = list(executor.map(lambda n: post(client, n), range(200)))
    assert answers == [(200, n) for n in range(200)]


# ---------------------------------------------------------------------------
# R1: results arrive without a polling delay
# ---------------------------------------------------------------------------


def test_a_result_reaches_the_caller_as_soon_as_the_worker_pushed_it(jobs, key, redis_url):
    """`poll_interval` is the back-off after an error only: an idle reader does not sleep it before reading."""
    queue_key = get_result_queue_key(key)

    async def scenario():
        client = _async_redis(redis_url)
        await start_result_reader(client, queue_key, poll_interval=1.0)
        try:
            instance = jobs(client)()
            delays = []
            for _ in range(8):
                # the action outlasts any read the reader started, so the result lands in an idle list
                stamped = await asyncio.wait_for(instance.stamp(delay=0.2), 10)
                delays.append(time.time() - stamped)
            return delays
        finally:
            await _stopped(queue_key)

    delays = asyncio.run(scenario())
    assert max(delays) < 0.05, delays


def test_results_waiting_when_the_reader_starts_are_all_delivered_in_list_order(jobs, key, redis_url, sync_redis):
    queue_key = get_result_queue_key(key)
    process_key = get_process_queue_key(queue_key)
    sync_redis.delete(process_key)

    async def scenario():
        client = _async_redis(redis_url)
        instance = jobs(client)()
        answered = []

        async def call(n):
            answered.append(await instance.echo(n=n))

        calls = [asyncio.create_task(call(n)) for n in range(50)]
        deadline = time.monotonic() + 30
        while sync_redis.llen(process_key) < 50 and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        waiting = [json.loads(item)["result"] for item in sync_redis.lrange(process_key, 0, -1)]
        await start_result_reader(client, queue_key, poll_interval=1.0)
        try:
            started = time.monotonic()
            await asyncio.wait_for(asyncio.gather(*calls), 5)
            return waiting, answered, time.monotonic() - started
        finally:
            await _stopped(queue_key)

    waiting, answered, elapsed = asyncio.run(scenario())
    assert sorted(waiting) == list(range(50))
    assert answered == waiting
    assert elapsed < 0.5


# ---------------------------------------------------------------------------
# R1: a reader cancelled while it waits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("how", ["cancel", "stop_result_reader"])
def test_a_reader_stopped_while_it_waits_stops_promptly_and_fails_pending_calls(jobs, key, redis_url, how):
    queue_key = get_result_queue_key(key)

    async def scenario():
        client = _async_redis(redis_url)
        reader = await start_result_reader(client, queue_key, poll_interval=1.0)
        call = asyncio.create_task(jobs(client)().slow(seconds=5))
        await asyncio.sleep(0.5)  # the reader waits on an empty list
        assert not reader.done()
        assert not call.done()
        started = time.monotonic()
        if how == "cancel":
            reader.cancel()
        else:
            await stop_result_reader(queue_key)
        await asyncio.wait({reader}, timeout=2)
        reader_stopped = time.monotonic() - started
        try:
            await asyncio.wait_for(call, 2)
        except ResultReaderStoppedError as error:
            outcome = (error.status_code, error.code, error.params)
        else:  # pragma: no cover - the assertion below reports it
            outcome = None
        call_failed = time.monotonic() - started
        await _stopped(queue_key)
        return reader.done(), reader_stopped, outcome, call_failed

    reader_done, reader_stopped, outcome, call_failed = asyncio.run(scenario())
    assert reader_done
    assert reader_stopped < 0.5
    assert outcome == (503, "fastapi-viewsets.result_reader_stopped", {"queue_key": queue_key})
    assert call_failed < 0.5


def test_a_waiting_reader_does_not_hold_up_other_commands_on_its_client(jobs, key, redis_url):
    queue_key = get_result_queue_key(key)

    async def scenario():
        client = _async_redis(redis_url)
        await start_result_reader(client, queue_key, poll_interval=1.0)
        try:
            await asyncio.sleep(0.3)  # the reader waits on an empty list
            started = time.monotonic()
            await asyncio.wait_for(client.set(f"{key}:probe", "1"), 1)
            value = await asyncio.wait_for(client.get(f"{key}:probe"), 1)
            elapsed = time.monotonic() - started
            answer = await asyncio.wait_for(jobs(client)().echo(n=7), 10)
            return value, elapsed, answer
        finally:
            await _stopped(queue_key)

    value, elapsed, answer = asyncio.run(scenario())
    assert value == b"1"
    assert elapsed < 0.2
    assert answer == 7


# ---------------------------------------------------------------------------
# R2: a push renews the list's expiry
# ---------------------------------------------------------------------------


def test_every_push_renews_the_result_lists_expiry(key, sync_redis):
    process_key = f"{key}:push"
    push_result(sync_redis, process_key, "first", result=1)
    assert 3500 < sync_redis.ttl(process_key) <= 3600
    sync_redis.expire(process_key, 10)
    push_result(sync_redis, process_key, "second", result=2)
    assert 3500 < sync_redis.ttl(process_key) <= 3600
    assert [json.loads(item)["correlation_id"] for item in sync_redis.lrange(process_key, 0, -1)] == ["first", "second"]


# ---------------------------------------------------------------------------
# P3: short argument reprs in the task message
# ---------------------------------------------------------------------------


def test_the_task_message_carries_short_argument_reprs(make_app, redis_url, key, sync_redis):
    """A call to a queue no worker consumes leaves its message in Redis, where its headers are read."""
    idle_queue = f"{key}-idle"
    sync_redis.delete(idle_queue)
    app = make_app("scenario-idle", redis_url, idle_queue)
    text = "x" * 200_000

    async def scenario():
        jobs_class = _client_class(app, key, _async_redis(redis_url))
        call = asyncio.create_task(jobs_class().size(text=text))
        deadline = time.monotonic() + 10
        while not sync_redis.llen(idle_queue) and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        call.cancel()
        await asyncio.gather(call, return_exceptions=True)
        return sync_redis.lrange(idle_queue, 0, -1)

    messages = asyncio.run(scenario())
    assert len(messages) == 1
    message = json.loads(messages[0])
    headers = message["headers"]
    assert headers["task"] == f"{key}.size"
    assert len(headers["argsrepr"]) < 200
    assert len(headers["kwargsrepr"]) < 200
    assert len(message["body"]) > len(text)


# ---------------------------------------------------------------------------
# P1: errors while publishing, eager mode
# ---------------------------------------------------------------------------


def test_a_redis_broker_that_cannot_be_reached_fails_the_call_with_send(make_app, key, redis_url):
    app = make_app("scenario-unreachable", "redis://127.0.0.1:1/0", f"{key}-unreachable")
    app.conf.task_publish_retry = False
    app.conf.broker_connection_retry = False
    app.conf.broker_connection_max_retries = 0
    app.conf.broker_connection_timeout = 1

    async def scenario():
        jobs_class = _client_class(app, key, _async_redis(redis_url))
        with pytest.raises(ActionDispatchError) as raised:
            await asyncio.wait_for(jobs_class().echo(n=1), 20)
        return raised.value

    error = asyncio.run(scenario())
    assert error.status_code == 503
    assert error.code == "fastapi-viewsets.action_dispatch_failed"
    assert error.params == {"task": f"{key}.echo", "reason": "send"}


def test_eager_mode_still_dispatches_to_the_worker(worker, make_app, redis_url, key):
    assert worker.poll() is None, "the worker exited"
    app = make_app("scenario-eager", redis_url, key)
    app.conf.task_always_eager = True
    queue_key = get_result_queue_key(key)

    async def scenario():
        client = _async_redis(redis_url)
        jobs_class = _client_class(app, key, client)
        await start_result_reader(client, queue_key)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")  # Celery warns that task_always_eager does not apply to sent tasks
                return await asyncio.wait_for(asyncio.gather(*(jobs_class().echo(n=n) for n in range(20))), 30)
        finally:
            await _stopped(queue_key)

    assert asyncio.run(scenario()) == list(range(20))
