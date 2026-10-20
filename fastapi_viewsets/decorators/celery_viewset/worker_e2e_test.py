"""
`celery_viewset` against a real Celery worker (prefork, and gevent where gevent is installed) and
the Redis named by FASTAPI_VIEWSETS_TEST_REDIS_URL (skipped without it), called over HTTP: what a
caller receives, and what `execute()`, `executor_started()` and `on_transition()` observe, when the
executor interrupts an execution or never runs it; and the events a standard and a custom write of
a scoped class publish from the worker.
"""

import asyncio
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import textwrap
import threading
import time
import uuid

from contextlib import asynccontextmanager

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from ...action_configuration import action_configuration, TimeLimits
from .. import route_viewset
from . import celery_viewset_client, get_result_queue_key, start_result_reader, stop_result_reader

pytestmark = pytest.mark.celery_worker

REPOSITORY = pathlib.Path(__file__).parents[3]

WORKER_MODULE = textwrap.dedent(
    """
    import asyncio
    import json
    import os
    import time

    import redis
    import redis.asyncio

    from celery import Celery
    from fastapi import APIRouter, Request

    from fastapi_viewsets.conf import settings
    from fastapi_viewsets.context import Context
    from fastapi_viewsets.decorators import celery_viewset_server
    from fastapi_viewsets.events import Change, ChangeEvents, event_scope, publish, RecordsChanged, write_topic
    from fastapi_viewsets.events.redis import RedisEventStore
    from fastapi_viewsets.middleware import Middleware

    URL = os.environ["E2E_REDIS_URL"]
    KEY = os.environ["E2E_KEY"]
    probe = redis.Redis.from_url(URL)

    app = Celery("e2e", broker=URL)
    app.conf.task_default_queue = KEY
    app.conf.worker_prefetch_multiplier = 1
    app.conf.worker_lost_wait = 1.0
    app.conf.broker_connection_retry_on_startup = True
    # the tasks' own acks_late=False and reject_on_worker_lost=False override it
    app.conf.task_acks_late = True
    app.conf.task_reject_on_worker_lost = True


    class Probe(Middleware):
        async def __call__(self, _request, _viewset, _context, call_next):
            return await call_next()

        async def executor_started(self):
            probe.rpush(f"{KEY}:started", json.dumps([os.getpid(), id(asyncio.get_running_loop())]))

        async def execute(self, execution, call_next):
            try:
                return await call_next()
            except BaseException as error:
                reason = getattr(error, "params", {}).get("reason")
                probe.rpush(f"{KEY}:execute", json.dumps([execution.action, type(error).__name__, reason]))
                raise

        async def on_transition(self, transition):
            reason = transition.error.params.get("reason") if transition.error is not None else None
            probe.rpush(f"{KEY}:transitions", json.dumps([transition.kind, transition.action, reason]))


    settings.viewsets_command_middleware = [Probe(), ChangeEvents()]
    settings.viewsets_event_store = RedisEventStore(redis.asyncio.Redis.from_url(URL), key_prefix=f"{KEY}:events:")


    class Jobs:
        __router = APIRouter()

        @__router.post("/suspended")
        async def suspended(self, seconds: float) -> str:
            await asyncio.sleep(seconds)
            return "done"

        @__router.post("/busy")
        async def busy(self, seconds: float) -> str:
            time.sleep(seconds)
            return "done"

        @__router.post("/hard")
        async def hard(self, seconds: float) -> str:
            time.sleep(seconds)
            return "done"

        @__router.post("/hard_suspended")
        async def hard_suspended(self, seconds: float) -> str:
            await asyncio.sleep(seconds)
            return "done"

        @__router.post("/long")
        async def long(self, seconds: float) -> str:
            await asyncio.sleep(seconds)
            return "done"

        @__router.post("/exit")
        async def exit(self) -> None:
            os._exit(1)

        @__router.post("/system_exit")
        async def system_exit(self) -> None:
            raise SystemExit(1)

        @__router.post("/quick")
        async def quick(self) -> str:
            return "quick"


    celery_viewset_server(app, KEY, redis_client=redis.Redis.from_url(URL))(Jobs)


    def no_key_here(_request, _context):
        raise RuntimeError("the web process carries the scope key")


    @event_scope(no_key_here, name=f"things-{KEY}")
    class Things:
        __router = APIRouter()
        pk_field_name = "id"

        @__router.post("")
        async def create(self, title: str) -> dict:
            return {"id": 7, "title": title}

        @__router.post("/{pk}/archive")
        async def archive(self, request: Request, context: Context, pk: int) -> None:
            topic = await write_topic(self, request, context)
            await publish(topic, RecordsChanged(op="deleted", changes=[Change(pk=pk)]))


    celery_viewset_server(app, f"{KEY}-things", redis_client=redis.Redis.from_url(URL))(Things)
    """
)


@pytest.fixture(scope="module")
def redis_url() -> str:
    url = os.environ.get("FASTAPI_VIEWSETS_TEST_REDIS_URL")
    if not url:
        pytest.skip("FASTAPI_VIEWSETS_TEST_REDIS_URL is not set")
    return url


POOLS = {
    "prefork": ["-P", "prefork", "-c", "1"],
    "gevent": ["-P", "gevent", "-c", "1"],
    "solo": ["-P", "solo"],
}


@pytest.fixture(scope="module")
def pool(request) -> str:
    return getattr(request, "param", "prefork")


in_gevent_pool = pytest.mark.parametrize(
    "pool",
    [
        pytest.param(
            "gevent",
            marks=pytest.mark.skipif(importlib.util.find_spec("gevent") is None, reason="gevent is not installed"),
        )
    ],
    indirect=True,
    scope="module",
)


@pytest.fixture(scope="module")
def key(pool) -> str:
    return f"e2e-{pool}-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def probe(redis_url, key):
    import redis

    client = redis.Redis.from_url(redis_url)
    yield client
    for suffix in ("started", "execute", "transitions"):
        client.delete(f"{key}:{suffix}")
    client.delete(key)


@pytest.fixture(scope="module")
def celery_app(redis_url, key):
    from celery import Celery

    app = Celery("e2e-client", broker=redis_url)
    app.conf.task_default_queue = key
    return app


@pytest.fixture(scope="module")
def worker(tmp_path_factory, redis_url, key, celery_app, pool):
    directory = tmp_path_factory.mktemp("worker")
    (directory / "e2e_worker.py").write_text(WORKER_MODULE)
    env = {
        **os.environ,
        "E2E_REDIS_URL": redis_url,
        "E2E_KEY": key,
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPOSITORY), str(directory), os.environ.get("PYTHONPATH")])),
    }
    command = [sys.executable, "-m", "celery", "-A", "e2e_worker", "worker", *POOLS[pool]]
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


def _node(key: str) -> str:
    import socket

    return f"{key}@{socket.gethostname()}"


@pytest.fixture(scope="module")
def client(worker, redis_url, key, celery_app):
    import redis.asyncio

    assert worker.poll() is None, "the worker exited"

    class Jobs:
        __router = APIRouter()

        @action_configuration({TimeLimits: TimeLimits(soft=0.5)})
        @__router.post("/suspended")
        async def suspended(self, seconds: float) -> str: ...

        @action_configuration({TimeLimits: TimeLimits(soft=0.5)})
        @__router.post("/busy")
        async def busy(self, seconds: float) -> str: ...

        @action_configuration({TimeLimits: TimeLimits(hard=1)})
        @__router.post("/hard")
        async def hard(self, seconds: float) -> str: ...

        @action_configuration({TimeLimits: TimeLimits(hard=1)})
        @__router.post("/hard_suspended")
        async def hard_suspended(self, seconds: float) -> str: ...

        @__router.post("/long")
        async def long(self, seconds: float) -> str: ...

        @__router.post("/exit")
        async def exit(self) -> None: ...

        @__router.post("/system_exit")
        async def system_exit(self) -> None: ...

        @__router.post("/quick")
        async def quick(self) -> str: ...

        @__router.post("/missing")
        async def missing(self) -> str: ...

    reader_client = redis.asyncio.Redis.from_url(redis_url)

    @asynccontextmanager
    async def lifespan(_app):
        await start_result_reader(reader_client, get_result_queue_key(key))
        yield
        await stop_result_reader(get_result_queue_key(key))

    router = APIRouter()
    route_viewset(router, "/jobs")(
        celery_viewset_client(celery_app, key, redis_client=reader_client, result_timeout=30)(Jobs)
    )
    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    with TestClient(app) as test_client:
        yield test_client


def _error(response) -> tuple[int, str, str | None]:
    body = response.json()
    return response.status_code, body["detail_code"], body["detail_params"].get("reason")


def _entries(probe, key, suffix) -> list:
    return [json.loads(item) for item in probe.lrange(f"{key}:{suffix}", 0, -1)]


def _eventually(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return predicate()


def _in_background(call):
    outcome = {}
    thread = threading.Thread(target=lambda: outcome.setdefault("response", call()))
    thread.start()
    return thread, outcome


def _task_ids(celery_app, key, which: str, name: str) -> list[str]:
    tasks = getattr(celery_app.control.inspect(timeout=1, destination=[_node(key)]), which)() or {}
    return [task["id"] for node_tasks in tasks.values() for task in node_tasks if task["name"] == name]


def test_a_soft_time_limit_while_suspended(client, probe, key):
    assert _error(client.post("/jobs/suspended", params={"seconds": 5})) == (
        504,
        "fastapi-viewsets.action_interrupted",
        "time_limit",
    )
    assert ["suspended", "ActionInterruptedError", "time_limit"] in _entries(probe, key, "execute")


def test_a_soft_time_limit_in_synchronous_code(client, probe, key):
    assert _error(client.post("/jobs/busy", params={"seconds": 5})) == (
        504,
        "fastapi-viewsets.action_interrupted",
        "time_limit",
    )
    assert ["busy", "ActionInterruptedError", "time_limit"] in _entries(probe, key, "execute")


def test_a_hard_time_limit(client, probe, key):
    assert _error(client.post("/jobs/hard", params={"seconds": 5})) == (
        504,
        "fastapi-viewsets.action_interrupted",
        "time_limit",
    )
    assert _eventually(lambda: ["interrupted", "hard", "time_limit"] in _entries(probe, key, "transitions"))
    # the process ends on SIGTERM, which unwinds as SystemExit where it gets the chance
    assert all(entry[1] == "SystemExit" for entry in _entries(probe, key, "execute") if entry[0] == "hard")


def test_a_lost_worker_process(client, probe, key):
    assert _error(client.post("/jobs/exit")) == (503, "fastapi-viewsets.action_interrupted", "lost")
    assert _eventually(lambda: ["interrupted", "exit", "lost"] in _entries(probe, key, "transitions"))


def test_a_terminated_execution(client, probe, key, celery_app):
    thread, outcome = _in_background(lambda: client.post("/jobs/long", params={"seconds": 30}))
    task_ids = _eventually(lambda: _task_ids(celery_app, key, "active", f"{key}.long"))
    celery_app.control.revoke(task_ids[0], terminate=True)
    thread.join(timeout=20)
    assert _error(outcome["response"]) == (503, "fastapi-viewsets.action_interrupted", "terminated")
    assert _eventually(lambda: ["interrupted", "long", "terminated"] in _entries(probe, key, "transitions"))
    # the suspended execution unwinds as the SystemExit of SIGTERM, and only the supervisor reports it
    assert ["long", "SystemExit", None] in _entries(probe, key, "execute")
    time.sleep(1)
    assert [entry for entry in _entries(probe, key, "transitions") if entry[1] == "long"] == [
        ["interrupted", "long", "terminated"]
    ]


def test_a_call_revoked_before_it_started(client, probe, key, celery_app):
    blocker, blocked = _in_background(lambda: client.post("/jobs/long", params={"seconds": 2}))
    _eventually(lambda: _task_ids(celery_app, key, "active", f"{key}.long"))
    thread, outcome = _in_background(lambda: client.post("/jobs/quick"))
    task_ids = _eventually(lambda: _task_ids(celery_app, key, "reserved", f"{key}.quick"))
    celery_app.control.revoke(task_ids[0])
    thread.join(timeout=20)
    blocker.join(timeout=20)
    assert blocked["response"].status_code == 200
    assert _error(outcome["response"]) == (503, "fastapi-viewsets.action_dispatch_failed", "revoked")
    assert _eventually(lambda: ["not_run", "quick", "revoked"] in _entries(probe, key, "transitions"))


def test_an_unknown_task(client, probe, key):
    assert _error(client.post("/jobs/missing")) == (503, "fastapi-viewsets.action_dispatch_failed", "unknown_task")
    assert _eventually(lambda: ["not_run", "missing", "unknown_task"] in _entries(probe, key, "transitions"))


def test_executor_started_runs_once_in_each_process(client, probe, key, worker):
    assert client.post("/jobs/quick").json() == "quick"
    started = [pid for pid, _loop in _entries(probe, key, "started")]
    assert len(started) == len(set(started))
    assert worker.pid in started
    # the supervisor, the first child, and a new child after each child the executor ended
    assert len(started) >= 3


# The gevent pool raises its hard time limit, `gevent.Timeout`, inside the execution's greenlet.


@in_gevent_pool
@pytest.mark.parametrize("action", ["hard", "hard_suspended"])
def test_a_hard_time_limit_in_the_gevent_pool(client, probe, key, action):
    assert _error(client.post(f"/jobs/{action}", params={"seconds": 5})) == (
        504,
        "fastapi-viewsets.action_interrupted",
        "time_limit",
    )
    assert [action, "ActionInterruptedError", "time_limit"] in _entries(probe, key, "execute")
    assert _eventually(lambda: ["interrupted", action, "time_limit"] in _entries(probe, key, "transitions"))
    assert client.post("/jobs/quick").json() == "quick"


@in_gevent_pool
def test_executor_started_runs_once_on_each_loop_of_the_gevent_pool(client, probe, key, worker):
    assert client.post("/jobs/quick").json() == "quick"
    started = [tuple(entry) for entry in _entries(probe, key, "started")]
    assert {pid for pid, _loop in started} == {worker.pid}
    assert len(started) == len(set(started))


# A process no supervisor outlives reports its own exit.


@pytest.mark.parametrize("pool", ["solo"], indirect=True, scope="module")
def test_a_process_exit_in_the_solo_pool(client, probe, key, worker):
    assert _error(client.post("/jobs/system_exit")) == (503, "fastapi-viewsets.action_interrupted", "terminated")
    assert _eventually(lambda: ["interrupted", "system_exit", "terminated"] in _entries(probe, key, "transitions"))
    worker.wait(timeout=15)


def test_writes_of_a_scoped_class_publish_from_the_worker_to_a_web_process_subscriber(
    worker, redis_url, key, celery_app, probe, monkeypatch
):
    import redis.asyncio

    from ...conf import settings
    from ...context import Context
    from ...events import ChangeEvents, event_scope
    from ...events.conftest import FakePeer
    from ...events.delivery import reset_subscriptions, subscribe
    from ...events.redis import RedisEventStore

    assert worker.poll() is None, "the worker exited"
    task_prefix = f"{key}-things"

    @event_scope(lambda request, _context: request.headers.get("x-org"), name=f"things-{key}")
    class Things:
        __router = APIRouter()
        pk_field_name = "id"

        @__router.post("")
        async def create(self, title: str) -> dict: ...

        @__router.post("/{pk}/archive")
        async def archive(self, pk: int) -> None: ...

    reader_client = redis.asyncio.Redis.from_url(redis_url)

    @asynccontextmanager
    async def lifespan(_app):
        await start_result_reader(reader_client, get_result_queue_key(task_prefix))
        yield
        await stop_result_reader(get_result_queue_key(task_prefix))

    router = APIRouter()
    route_viewset(router, "/things", pk_field_name="id")(
        celery_viewset_client(celery_app, task_prefix, redis_client=reader_client, result_timeout=30)(Things)
    )
    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    monkeypatch.setattr(settings, "viewsets_command_middleware", [ChangeEvents()])

    async def scenario(client: TestClient) -> tuple[FakePeer, FakePeer]:
        web = RedisEventStore(redis.asyncio.Redis.from_url(redis_url), key_prefix=f"{key}:events:")
        monkeypatch.setattr(settings, "viewsets_event_store", web)
        await web.start()
        try:
            member, stranger = FakePeer(), FakePeer()
            await subscribe(member, Context({}), "/things", store_topic=f"@things-{key}:7", scope="7")
            await subscribe(stranger, Context({}), "/things", store_topic=f"@things-{key}:8", scope="8")
            response = await asyncio.to_thread(client.post, "/things", params={"title": "x"}, headers={"x-org": "7"})
            assert response.json() == {"id": 7, "title": "x"}
            response = await asyncio.to_thread(client.post, "/things/3/archive", headers={"x-org": "7"})
            assert response.status_code == 200
            deadline = time.monotonic() + 10
            while len(member.pushes) < 2 and time.monotonic() < deadline:
                await asyncio.sleep(0.05)
            return member, stranger
        finally:
            await web.stop()
            reset_subscriptions()

    with TestClient(app) as client:
        member, stranger = asyncio.run(scenario(client))
    assert [payload for payload, _headers in member.pushes] == [
        {"op": "created", "changes": [{"pk": 7}]},
        {"op": "deleted", "changes": [{"pk": 3}]},
    ]
    assert [(headers[":topic"], headers[":scope"]) for _payload, headers in member.pushes] == [("/things", "7")] * 2
    assert stranger.pushes == []
    assert [entry for entry in _entries(probe, key, "execute") if entry[0] in ("create", "archive")] == []
    for stored in probe.scan_iter(match=f"{key}:events:*"):
        probe.delete(stored)
