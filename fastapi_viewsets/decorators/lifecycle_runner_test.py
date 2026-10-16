import asyncio
import signal

from unittest.mock import MagicMock

import pytest

from fastapi import APIRouter, FastAPI, Request
from fastapi.testclient import TestClient

from ..action_configuration import action_configuration
from ..conf import settings
from ..context import Context
from ..middleware import ActionExecution, Middleware
from .celery_viewset import celery_viewset_client, celery_viewset_server, result_reader
from .lifecycle_runner import current_call, lifecycle_runner
from .route_viewset import route_viewset


class Recorder(Middleware):
    def __init__(self, name: str, events: list):
        self.name = name
        self.events = events

    async def __call__(self, _request, _viewset, _context, call_next):
        return await call_next()

    async def execute(self, execution: ActionExecution, call_next):
        self.events.append((self.name, "enter", execution))
        try:
            result = await call_next()
        except BaseException as error:
            self.events.append((self.name, "error", error))
            raise
        self.events.append((self.name, "exit", result))
        return result


@pytest.fixture
def events(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        settings, "viewsets_command_middleware", [Recorder("outer", recorded), Recorder("inner", recorded)]
    )
    return recorded


def kinds(events) -> list[tuple[str, str]]:
    return [(name, kind) for name, kind, _ in events]


def test_execute_wraps_an_in_process_action_with_everything_about_the_call(events):
    router = APIRouter()

    @route_viewset(router, "/things")
    class ThingViewSet:
        __router = APIRouter()

        @__router.post("/{pk}")
        async def create(self, pk: int) -> dict:
            events.append(("action", "run", None))
            return {"pk": pk}

    app = FastAPI()
    app.include_router(router)
    assert TestClient(app).post("/things/3?x=1").json() == {"pk": 3}

    assert kinds(events) == [
        ("outer", "enter"),
        ("inner", "enter"),
        ("action", "run"),
        ("inner", "exit"),
        ("outer", "exit"),
    ]
    execution = events[0][2]
    assert (execution.viewset_class, execution.action) == (ThingViewSet, "create")
    assert isinstance(execution.viewset, ThingViewSet)
    assert (execution.args, execution.kwargs) == ((), {"pk": 3})
    assert isinstance(execution.context, Context)
    assert execution.request.url.path == "/things/3"
    assert execution.parent is None
    assert events[3][2] == {"pk": 3}


def test_execute_sees_and_may_replace_the_outcome(monkeypatch):
    class Replacing(Middleware):
        async def __call__(self, _request, _viewset, _context, call_next):
            return await call_next()

        async def execute(self, _execution, call_next):
            return {"wrapped": await call_next()}

    monkeypatch.setattr(settings, "viewsets_command_middleware", [Replacing()])

    class Plain:
        async def act(self):
            return 1

    assert asyncio.run(lifecycle_runner(Plain.act, Plain(), Plain)) == {"wrapped": 1}


def test_execute_sees_the_exception_of_the_action(events):
    class Failing:
        async def act(self):
            raise ValueError("no")

    with pytest.raises(ValueError, match="no"):
        asyncio.run(lifecycle_runner(Failing.act, Failing(), Failing))
    assert kinds(events) == [("outer", "enter"), ("inner", "enter"), ("inner", "error"), ("outer", "error")]


def test_a_function_middleware_has_no_execute_and_is_skipped(monkeypatch):
    async def function_middleware(_request, _viewset, _context, call_next):
        return await call_next()

    monkeypatch.setattr(settings, "viewsets_command_middleware", [function_middleware])

    class Plain:
        async def act(self):
            return current_call().execution.action

    assert asyncio.run(lifecycle_runner(Plain.act, Plain(), Plain)) == "act"


def test_the_given_context_is_bound_to_the_actions_configuration(events):
    @action_configuration({Recorder: "configured"})
    class Worker:
        async def act(self):
            return "done"

    context = Context({"token": "t"})
    assert asyncio.run(lifecycle_runner(Worker.act, Worker(), Worker, context=context)) == "done"
    execution = events[0][2]
    assert execution.context.raw() is context.raw()
    assert execution.context.configuration_for(Recorder) == "configured"
    assert execution.kwargs == {}


# ---------------------------------------------------------------------------
# an action handed to an executor
# ---------------------------------------------------------------------------


class _Executor:
    """celery_viewset_client and celery_viewset_server in one process: the client's send_task runs
    the server's task on the thread send_task is called from, and resolves the client's future."""

    def __init__(self, monkeypatch):
        self.tasks = {}
        self.futures = {}
        self.server_app = MagicMock()
        self.server_app.task.side_effect = lambda name, **_kwargs: lambda func: self.tasks.setdefault(name, func)
        self.client_app = MagicMock()
        self.client_app.send_task.side_effect = self.send_task
        self.sent = []
        monkeypatch.setattr(result_reader, "register_future", self.register)

    def register(self, correlation_id, future, _queue_key=None):
        self.futures[correlation_id] = future

    def send_task(self, name, args, kwargs, **_options):
        self.sent.append(kwargs)
        future = self.futures[kwargs["_correlation_id"]]
        result = self.tasks[name](*args, **{k: v for k, v in kwargs.items() if k != "_result_queue_key"})
        future.get_loop().call_soon_threadsafe(future.set_result, result)


def _viewset_pair(executor: _Executor, events: list):
    class Base:
        __router = APIRouter()

        @__router.post("")
        async def create(self) -> str:
            events.append(("action", "run", current_call()))
            return "created"

    worker = celery_viewset_server(celery_app=executor.server_app, task_prefix="things")(type("Worker", (Base,), {}))
    client = celery_viewset_client(celery_app=executor.client_app, task_prefix="things", redis_client=MagicMock())(
        type("Client", (Base,), {})
    )
    return worker, client


def test_execute_runs_once_for_a_deferred_action_and_where_it_executes(events, monkeypatch):
    executor = _Executor(monkeypatch)
    _worker, client = _viewset_pair(executor, events)

    class Override(client):
        async def create(self) -> str:
            events.append(("override", "run", None))
            return await super().create()

    router = APIRouter()
    route_viewset(router, "/things")(Override)
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).post("/things", headers={"x-call": "k"})
    assert response.json() == "created"

    assert kinds(events) == [
        ("override", "run"),
        ("outer", "enter"),
        ("inner", "enter"),
        ("action", "run"),
        ("inner", "exit"),
        ("outer", "exit"),
    ]
    execution = events[1][2]
    assert execution.viewset_class.__name__ == "Worker"
    assert execution.request.headers["x-call"] == "k"
    assert execution.request.url.path == "/things"
    assert execution.parent is None


def test_a_deferred_action_receives_the_calls_context_without_declaring_it(events, monkeypatch):
    executor = _Executor(monkeypatch)
    _worker, client = _viewset_pair(executor, events)

    async def processor(_request, _viewset):
        return {"user": "u"}

    monkeypatch.setattr(settings, "viewsets_context_processors", [processor])
    router = APIRouter()
    route_viewset(router, "/things")(client)
    app = FastAPI()
    app.include_router(router)
    TestClient(app).post("/things")

    execution = events[0][2]
    assert execution.context.raw() == {"user": "u"}
    assert execution.kwargs == {}


def test_an_action_dispatched_inside_an_execution_has_it_as_parent(events, monkeypatch):
    executor = _Executor(monkeypatch)
    _worker, client = _viewset_pair(executor, events)

    class Outer:
        async def act(self):
            return await client().create()

    request = Request({"type": "http", "method": "GET", "path": "/outer", "headers": []})
    assert asyncio.run(lifecycle_runner(Outer.act, Outer(), Outer, request=request)) == "created"

    inner = next(execution for name, kind, execution in events if kind == "enter" and execution.action == "create")
    assert inner.parent.viewset_class == f"{Outer.__module__}.{Outer.__qualname__}"
    assert inner.parent.action == "act"
    assert inner.request.url.path == "/outer"


def test_an_interrupted_execution_unwinds_through_execute(events):
    executor = MagicMock()
    tasks = {}
    executor.task.side_effect = lambda name, **_kwargs: lambda func: tasks.setdefault(name, func)

    class TimeLimitError(Exception):
        pass

    class Slow:
        __router = APIRouter()

        @__router.post("")
        async def slow(self) -> None:
            await asyncio.sleep(10)

    celery_viewset_server(celery_app=executor, task_prefix="slow")(Slow)

    def interrupt(_signum, _frame):
        raise TimeLimitError

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, 0.05)
    try:
        with pytest.raises(TimeLimitError):
            tasks["slow.slow"]()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)

    assert kinds(events) == [("outer", "enter"), ("inner", "enter"), ("inner", "error"), ("outer", "error")]
    assert isinstance(events[2][2], asyncio.CancelledError)
