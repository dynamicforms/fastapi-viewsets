"""
`celery_viewset` with `include` / `exclude`: over HTTP against a real Celery worker and the Redis of
FASTAPI_VIEWSETS_TEST_REDIS_URL (skipped without it), the selected actions run in the worker and the others in
the web process, with one route table and one /schema. Decoration errors are checked without a worker.

The worker imports this module for `define_reports`, so the web process and the worker build the same class.
"""

import json
import os
import re
import subprocess
import sys
import textwrap
import time
import uuid

from collections.abc import Callable
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ...action_configuration import action_configuration, TimeLimits
from ...conf import settings
from ...context import Context
from ...middleware import Middleware
from ...mixins import ListMixin, RetrieveMixin
from .. import route_viewset
from . import (
    celery_viewset,
    celery_viewset_client,
    celery_viewset_server,
    get_result_queue_key,
    start_result_reader,
    stop_result_reader,
)
from .worker_e2e_test import _node, REPOSITORY

celery_worker = pytest.mark.celery_worker

LOCAL_ACTIONS = ("list_items", "retrieve", "stamp", "summary")
DEFERRED_ACTIONS = ("export_pdf", "export_bundle")

VARIANTS: dict[str, dict] = {
    "exclude": {"exclude": list(LOCAL_ACTIONS)},
    "include": {"include": list(DEFERRED_ACTIONS)},
    "pattern": {"include": [re.compile("export_.*")]},
}
"""The same split of `define_reports`' actions, selected three ways."""


def here() -> dict:
    """The process this code runs in: the worker's environment sets E2E_PROCESS."""
    return {"process": os.environ.get("E2E_PROCESS", "web"), "pid": os.getpid()}


class Record(BaseModel):
    id: int
    process: str
    pid: int


def define_reports(
    name: str, decorate: Callable[[type], type], *, time_limited: tuple[str, ...] = (), class_time_limits: bool = False
) -> type:
    """
    A viewset with list, retrieve and custom actions that answer the process they ran in. `time_limited` names its
    own methods that get a method-level TimeLimits; `class_time_limits` sets one on the class.
    """

    class Reports(ListMixin[Record], RetrieveMixin[int, Record]):
        __router = APIRouter()
        list_shape = "plain"

        async def perform_list(self, _context: Context) -> list[Record]:
            return [Record(id=1, **here()), Record(id=2, **here())]

        async def perform_retrieve(self, _context: Context, pk: int) -> Record:
            return Record(id=pk, **here())

        @__router.post("/{pk}/export_pdf")
        async def export_pdf(self, pk: int) -> dict:
            return {"pk": pk, **here()}

        @__router.post("/{pk}/export_bundle")
        async def export_bundle(self, pk: int) -> dict:
            return {"pk": pk, **here(), "stamp": await self.stamp(pk=pk)}

        @__router.post("/{pk}/stamp")
        async def stamp(self, pk: int) -> dict:
            return {"pk": pk, **here()}

        @__router.post("/{pk}/summary")
        async def summary(self, pk: int) -> dict:
            return {"pk": pk, **here(), "export": await self.export_pdf(pk=pk)}

    for method_name in time_limited:
        action_configuration({TimeLimits: TimeLimits(soft=1)})(vars(Reports)[method_name])
    named = type(name, (Reports,), {"__module__": __name__})
    if class_time_limits:
        action_configuration({TimeLimits: TimeLimits(soft=1)})(named)
    return decorate(named)


class Probe(Middleware):
    """Records every middleware hook as a list through `record`."""

    def __init__(self, record: Callable[[list], None]):
        self.record = record

    async def __call__(self, _request, _viewset, _context, call_next):
        return await call_next()

    async def executor_started(self):
        self.record(["started", here()["process"]])

    async def execute(self, execution, call_next):
        self.record(["execute", execution.viewset_class.__name__, execution.action, here()["process"]])
        return await call_next()

    async def on_transition(self, transition):
        self.record(["transition", transition.kind, transition.action])


WORKER_MODULE = textwrap.dedent(
    """
    import json
    import os

    import redis

    from celery import Celery

    from fastapi_viewsets.conf import settings
    from fastapi_viewsets.decorators import celery_viewset_server
    from fastapi_viewsets.decorators.celery_viewset.include_exclude_e2e_test import Probe, VARIANTS, define_reports

    URL = os.environ["E2E_REDIS_URL"]
    KEY = os.environ["E2E_KEY"]
    probe = redis.Redis.from_url(URL)

    app = Celery("e2e-include", broker=URL)
    app.conf.task_default_queue = KEY
    app.conf.worker_prefetch_multiplier = 1
    app.conf.broker_connection_retry_on_startup = True

    settings.viewsets_command_middleware = [Probe(lambda entry: probe.rpush(f"{KEY}:hooks", json.dumps(entry)))]

    for variant, selection in VARIANTS.items():
        define_reports(
            f"Reports{variant.title()}",
            celery_viewset_server(app, f"{KEY}-{variant}", redis_client=redis.Redis.from_url(URL), **selection),
        )
    """
)


@pytest.fixture(scope="module")
def redis_url() -> str:
    url = os.environ.get("FASTAPI_VIEWSETS_TEST_REDIS_URL")
    if not url:
        pytest.skip("FASTAPI_VIEWSETS_TEST_REDIS_URL is not set")
    return url


@pytest.fixture(scope="module")
def key() -> str:
    return f"e2e-include-{uuid.uuid4().hex[:8]}"


@pytest.fixture(scope="module")
def probe(redis_url, key):
    import redis

    client = redis.Redis.from_url(redis_url)
    yield client
    client.delete(f"{key}:hooks", key)


@pytest.fixture(scope="module")
def celery_app(redis_url, key):
    from celery import Celery

    app = Celery("e2e-include-client", broker=redis_url)
    app.conf.task_default_queue = key
    return app


@pytest.fixture(scope="module")
def worker(tmp_path_factory, redis_url, key, celery_app):
    directory = tmp_path_factory.mktemp("worker")
    (directory / "e2e_include_worker.py").write_text(WORKER_MODULE)
    env = {
        **os.environ,
        "E2E_REDIS_URL": redis_url,
        "E2E_KEY": key,
        "E2E_PROCESS": "worker",
        "PYTHONPATH": os.pathsep.join(filter(None, [str(REPOSITORY), str(directory), os.environ.get("PYTHONPATH")])),
    }
    command = [sys.executable, "-m", "celery", "-A", "e2e_include_worker", "worker", "-P", "prefork", "-c", "1"]
    log = (directory / "worker.log").open("w")
    process = subprocess.Popen(  # noqa: S603 - the interpreter running the tests
        [*command, "--loglevel=INFO", "-n", f"{key}@%h"], cwd=directory, env=env, stdout=log, stderr=subprocess.STDOUT
    )
    process.log_path = directory / "worker.log"
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


@pytest.fixture(scope="module")
def web_hooks() -> list:
    hooks: list = []
    saved = settings.viewsets_command_middleware
    settings.viewsets_command_middleware = [Probe(hooks.append)]
    yield hooks
    settings.viewsets_command_middleware = saved


@pytest.fixture(scope="module")
def client(worker, redis_url, key, celery_app, web_hooks):  # noqa: ARG001 - installs the web Probe
    import redis.asyncio

    assert worker.poll() is None, "the worker exited"
    reader_client = redis.asyncio.Redis.from_url(redis_url)
    queue_keys = [get_result_queue_key(f"{key}-{variant}") for variant in VARIANTS]

    @asynccontextmanager
    async def lifespan(_app):
        for queue_key in queue_keys:
            await start_result_reader(reader_client, queue_key)
        yield
        for queue_key in queue_keys:
            await stop_result_reader(queue_key)

    router = APIRouter()
    for variant, selection in VARIANTS.items():
        decorate = celery_viewset_client(
            celery_app, f"{key}-{variant}", redis_client=reader_client, result_timeout=30, **selection
        )
        route_viewset(router, f"/{variant}")(define_reports(f"Reports{variant.title()}", decorate))
    app = FastAPI(lifespan=lifespan)
    app.include_router(router)
    app.state.viewsets_router = router
    with TestClient(app) as test_client:
        yield test_client


def _worker_hooks(probe, key) -> list:
    return [json.loads(item) for item in probe.lrange(f"{key}:hooks", 0, -1)]


WEB = {"process": "web", "pid": os.getpid()}

all_variants = pytest.mark.parametrize("variant", list(VARIANTS))


def _in_worker(answer: dict) -> bool:
    return answer["process"] == "worker" and answer["pid"] != os.getpid()


@celery_worker
@all_variants
def test_list_and_retrieve_answer_from_the_web_process(client, variant):
    listed = client.get(f"/{variant}")
    assert listed.status_code == 200, listed.text
    assert listed.json() == [{"id": 1, **WEB}, {"id": 2, **WEB}]

    retrieved = client.get(f"/{variant}/5")
    assert retrieved.status_code == 200, retrieved.text
    assert retrieved.json() == {"id": 5, **WEB}


@celery_worker
@all_variants
def test_a_selected_action_runs_in_the_worker(client, variant):
    response = client.post(f"/{variant}/7/export_pdf")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pk"] == 7
    assert _in_worker(body), body


@celery_worker
@all_variants
def test_an_action_left_out_runs_in_the_web_process(client, variant):
    response = client.post(f"/{variant}/4/stamp")
    assert response.status_code == 200, response.text
    assert response.json() == {"pk": 4, **WEB}


@celery_worker
@all_variants
def test_a_local_action_calling_a_selected_action_gets_the_workers_answer(client, variant):
    response = client.post(f"/{variant}/3/summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert {"pk": body["pk"], "process": body["process"], "pid": body["pid"]} == {"pk": 3, **WEB}
    assert body["export"]["pk"] == 3
    assert _in_worker(body["export"]), body


@celery_worker
@all_variants
def test_a_selected_action_calling_a_local_action_runs_it_in_the_worker(client, variant):
    response = client.post(f"/{variant}/9/export_bundle")
    assert response.status_code == 200, response.text
    body = response.json()
    assert _in_worker(body), body
    assert body["stamp"] == {"pk": 9, "process": "worker", "pid": body["pid"]}


@celery_worker
@all_variants
def test_an_action_left_out_runs_the_command_middleware_once_in_the_web_process(client, variant, web_hooks, probe, key):
    name = f"Reports{variant.title()}"
    web_hooks.clear()
    assert client.get(f"/{variant}/2").status_code == 200
    assert client.post(f"/{variant}/2/stamp").status_code == 200
    assert web_hooks == [["execute", name, "retrieve", "web"], ["execute", name, "stamp", "web"]]

    assert client.post(f"/{variant}/2/export_pdf").status_code == 200
    worker_hooks = _worker_hooks(probe, key)
    assert ["execute", name, "export_pdf", "worker"] in worker_hooks
    local = [
        entry for entry in worker_hooks if entry[0] == "execute" and entry[1] == name and entry[2] in LOCAL_ACTIONS
    ]
    assert local == []
    assert [entry for entry in worker_hooks if entry[0] == "transition" and entry[2] in LOCAL_ACTIONS] == []
    assert [entry for entry in web_hooks if entry[0] == "started"] == []
    assert [entry for entry in web_hooks if entry[0] == "transition" and entry[2] in LOCAL_ACTIONS] == []


def _operations(schema: dict) -> list[tuple[str, str]]:
    return sorted(
        (path, method)
        for path, item in schema["paths"].items()
        for method in item
        if method in ("get", "post", "put", "patch", "delete")
    )


def _routes(router: APIRouter, prefix: str) -> list[tuple[str, tuple[str, ...]]]:
    return sorted(
        (route.path, tuple(sorted(getattr(route, "methods", ()) or ())))
        for route in router.routes
        if route.path.startswith(prefix)
    )


@celery_worker
@all_variants
def test_schema_lists_every_action_once_at_unchanged_urls(client, variant):
    plain_router = APIRouter()
    route_viewset(plain_router, f"/{variant}")(define_reports(f"Reports{variant.title()}", lambda cls: cls))
    plain_app = FastAPI()
    plain_app.include_router(plain_router)
    with TestClient(plain_app) as plain_client:
        expected = plain_client.get(f"/{variant}/schema").json()

    response = client.get(f"/{variant}/schema")
    assert response.status_code == 200, response.text
    operations = _operations(response.json())
    assert operations == _operations(expected)
    assert len(operations) == len(set(operations))
    for path, method in [
        (f"/{variant}", "get"),
        (f"/{variant}/{{pk}}", "get"),
        *((f"/{variant}/{{pk}}/{action}", "post") for action in ("export_pdf", "export_bundle", "stamp", "summary")),
    ]:
        assert (path, method) in operations

    served = _routes(client.app.state.viewsets_router, f"/{variant}")
    assert served
    assert served == _routes(plain_router, f"/{variant}")


# Decoration errors: no worker, no Redis.


DECORATORS = {
    "celery_viewset": lambda **selection: celery_viewset(MagicMock(), "sel", redis_client=MagicMock(), **selection),
    "client": lambda **selection: celery_viewset_client(MagicMock(), "sel", redis_client=MagicMock(), **selection),
    "server": lambda **selection: celery_viewset_server(MagicMock(), "sel", redis_client=MagicMock(), **selection),
}

all_decorators = pytest.mark.parametrize("decorator", list(DECORATORS))


@all_decorators
@pytest.mark.parametrize("parameter", ["include", "exclude"])
def test_an_unknown_action_name_is_refused_naming_it_and_the_actions(decorator, parameter):
    with pytest.raises(ValueError, match="list_itemz") as raised:
        define_reports("Reports", DECORATORS[decorator](**{parameter: ["list_itemz"]}))
    for action in (*LOCAL_ACTIONS, *DEFERRED_ACTIONS):
        assert action in str(raised.value)


@all_decorators
def test_a_pattern_that_matches_no_action_is_refused(decorator):
    with pytest.raises(ValueError, match="import_"):
        define_reports("Reports", DECORATORS[decorator](include=[re.compile("import_.*")]))


@all_decorators
def test_a_plain_string_is_an_exact_name_not_a_pattern(decorator):
    with pytest.raises(ValueError, match=re.escape("export_.*")):
        define_reports("Reports", DECORATORS[decorator](include=["export_.*"]))


@all_decorators
def test_a_pattern_must_match_the_whole_name(decorator):
    with pytest.raises(ValueError, match="export"):
        define_reports("Reports", DECORATORS[decorator](include=[re.compile("export")]))


@all_decorators
def test_include_and_exclude_together_are_refused(decorator):
    with pytest.raises(ValueError, match="(?s)include.*exclude|exclude.*include"):
        define_reports("Reports", DECORATORS[decorator](include=["export_pdf"], exclude=["list_items"]))


@all_decorators
@pytest.mark.parametrize(("method_name", "action"), [("perform_retrieve", "retrieve"), ("stamp", "stamp")])
def test_a_method_level_time_limit_on_an_action_left_out_is_refused(decorator, method_name, action):
    with pytest.raises(ValueError, match=action):
        define_reports("Reports", DECORATORS[decorator](exclude=[action]), time_limited=(method_name,))


@all_decorators
def test_a_method_level_time_limit_on_a_selected_action_is_accepted(decorator):
    define_reports("Reports", DECORATORS[decorator](include=["export_pdf"]), time_limited=("export_pdf",))


@all_decorators
def test_a_class_level_time_limit_with_actions_left_out_is_accepted(decorator):
    define_reports("Reports", DECORATORS[decorator](exclude=["list_items", "retrieve"]), class_time_limits=True)
