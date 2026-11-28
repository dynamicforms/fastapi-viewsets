"""`celery_viewset` with `include` / `exclude`: only the selected actions run in the worker, the others
run in the FastAPI process as ordinary viewset actions."""

import asyncio
import re

from unittest.mock import MagicMock

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ...action_configuration import action_configuration, TimeLimits
from ...conf import settings
from ...context import Context, to_jsonable
from ...middleware import Middleware
from ...mixins import ListMixin
from ...mux_ws.registry import reset_registry
from .. import route_viewset, ViewSetRouter
from ..lifecycle_runner import DEFERRED_ACTIONS_ATTRIBUTE
from . import celery_viewset, celery_viewset_client, celery_viewset_server, result_reader, set_is_celery_worker


class Row(BaseModel):
    id: int
    name: str


class Probe(Middleware):
    def __init__(self):
        self.seen: list[tuple] = []

    async def __call__(self, _request, viewset, context, call_next):
        self.seen.append(("call", type(viewset).__name__, context.action_name))
        return await call_next()

    async def execute(self, execution, call_next):
        self.seen.append(("execute", execution.viewset_class.__name__, execution.action))
        return await call_next()

    async def executor_started(self):
        self.seen.append(("started",))

    async def on_transition(self, transition):
        self.seen.append(("transition", transition.kind, transition.action))


@pytest.fixture(autouse=True)
def _reset():
    saved = settings.viewsets_command_middleware
    reset_registry()
    yield
    settings.viewsets_command_middleware = saved
    reset_registry()
    set_is_celery_worker(None)


@pytest.fixture
def probe():
    probe = Probe()
    settings.viewsets_command_middleware = [probe]
    return probe


def report_class(decorate, base=ListMixin):
    """A viewset with `list_items` and the custom actions `heavy`, `light` (calls `heavy`) and `chain`
    (calls `light`)."""

    @decorate
    class ReportViewSet(base[Row]):
        __router = APIRouter()

        async def perform_list(self, _context) -> list[Row]:
            return [Row(id=1, name="Ana")]

        @__router.get("heavy")
        async def heavy(self, context: Context) -> Row:  # noqa: ARG002 - injected by name
            return Row(id=2, name="heavy")

        @__router.get("light")
        async def light(self, context: Context) -> list[Row]:
            return [Row(id=3, name="light"), await self.heavy(context=context)]

        @__router.get("chain")
        async def chain(self, context: Context) -> list[Row]:
            return await self.light(context=context)

    return ReportViewSet


def worker_app() -> tuple[MagicMock, dict]:
    app, tasks = MagicMock(), {}

    def register_task(name, **_options):
        def decorator(function):
            tasks[name] = function
            return function

        return decorator

    app.task.side_effect = register_task
    return app, tasks


def connected_client_app(monkeypatch, tasks: dict) -> MagicMock:
    """A client Celery app whose `send_task` runs the worker's task and resolves the caller's future."""
    futures: dict[str, asyncio.Future] = {}
    monkeypatch.setattr(
        result_reader,
        "register_future",
        lambda correlation_id, future, _queue_key=None: futures.__setitem__(correlation_id, future),
    )

    def send_task(name, kwargs, **_options):
        correlation_id = kwargs.pop("_correlation_id")
        kwargs.pop("_result_queue_key")
        result = tasks[name](**kwargs)
        future = futures[correlation_id]
        future.get_loop().call_soon_threadsafe(future.set_result, to_jsonable(result))

    app = MagicMock()
    app.send_task.side_effect = send_task
    return app


def served(viewset_class) -> TestClient:
    router = APIRouter()
    route_viewset(router, "/reports")(viewset_class)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def client_decorator(**selection):
    return celery_viewset_client(celery_app=MagicMock(), task_prefix="reports", redis_client=MagicMock(), **selection)


def server_decorator(app=None, **selection):
    return celery_viewset_server(
        celery_app=app or worker_app()[0], task_prefix="reports", redis_client=MagicMock(), **selection
    )


# ---------------------------------------------------------------------------
# Selection: which methods are patched, which tasks registered
# ---------------------------------------------------------------------------


def test_without_selection_every_action_is_deferred():
    cls = report_class(client_decorator())
    assert getattr(cls, DEFERRED_ACTIONS_ATTRIBUTE) == {"list_items", "heavy", "light", "chain"}


def test_exclude_leaves_the_excluded_methods_unpatched():
    original = report_class(lambda c: c)
    cls = report_class(client_decorator(exclude=["list_items", "light", "chain"]))
    assert getattr(cls, DEFERRED_ACTIONS_ATTRIBUTE) == {"heavy"}
    assert cls.list_items is original.list_items
    assert cls.light.__code__ is original.light.__code__
    assert cls.heavy.__code__ is not original.heavy.__code__


def test_include_with_a_pattern_selects_by_fullmatch():
    cls = report_class(client_decorator(include=[re.compile("he.*")]))
    assert getattr(cls, DEFERRED_ACTIONS_ATTRIBUTE) == {"heavy"}


def test_the_worker_registers_tasks_for_the_deferred_actions_only():
    app, tasks = worker_app()
    report_class(server_decorator(app, exclude=[re.compile("l.*"), "chain"]))
    assert set(tasks) == {"reports.heavy"}


def test_the_worker_registers_the_included_actions():
    app, tasks = worker_app()
    report_class(server_decorator(app, include=("heavy", "list_items")))
    assert set(tasks) == {"reports.heavy", "reports.list_items"}


def test_celery_viewset_passes_the_selection_to_both_sides():
    set_is_celery_worker(False)
    cls = report_class(celery_viewset(MagicMock(), "reports", redis_client=MagicMock(), include=["heavy"]))
    assert getattr(cls, DEFERRED_ACTIONS_ATTRIBUTE) == {"heavy"}

    set_is_celery_worker(True)
    app, tasks = worker_app()
    report_class(celery_viewset(app, "reports", redis_client=MagicMock(), exclude=["heavy"]))
    assert set(tasks) == {"reports.list_items", "reports.light", "reports.chain"}


# ---------------------------------------------------------------------------
# ValueErrors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("factory", [client_decorator, server_decorator])
def test_include_and_exclude_together_raise(factory):
    with pytest.raises(ValueError, match="include or exclude"):
        factory(include=["heavy"], exclude=["light"])


def test_celery_viewset_raises_for_include_and_exclude_together():
    with pytest.raises(ValueError, match="include or exclude"):
        celery_viewset(MagicMock(), "reports", redis_client=MagicMock(), include=["heavy"], exclude=["light"])


@pytest.mark.parametrize("factory", [client_decorator, server_decorator])
@pytest.mark.parametrize("selection", ["include", "exclude"])
def test_an_unknown_name_raises_and_lists_the_actions(factory, selection):
    with pytest.raises(ValueError, match=r"'export_pdf'.*ReportViewSet.*chain, heavy, light, list_items"):
        report_class(factory(**{selection: ["heavy", "export_pdf"]}))


@pytest.mark.parametrize("factory", [client_decorator, server_decorator])
def test_a_pattern_that_matches_nothing_raises(factory):
    with pytest.raises(ValueError, match="'export_.*'"):
        report_class(factory(exclude=[re.compile("export_.*")]))


def test_a_string_entry_is_an_exact_name_not_a_pattern():
    with pytest.raises(ValueError, match="'he.*'"):
        report_class(client_decorator(include=["he.*"]))


def test_a_plain_string_as_the_selection_raises():
    with pytest.raises(ValueError, match="iterable"):
        client_decorator(include="heavy")


def timed_class(decorate, target: str):
    """A viewset whose `light` action (target "action") or `perform_list` (target "perform") has TimeLimits."""
    limits = action_configuration({TimeLimits: TimeLimits(soft=5)})
    light_decorator = limits if target == "action" else (lambda f: f)
    perform_decorator = limits if target == "perform" else (lambda f: f)

    @decorate
    class TimedViewSet(ListMixin[Row]):
        __router = APIRouter()

        @perform_decorator
        async def perform_list(self, _context) -> list[Row]:
            return []

        @__router.get("light")
        @light_decorator
        async def light(self, context: Context) -> Row:  # noqa: ARG002 - injected by name
            return Row(id=1, name="light")

    return TimedViewSet


@pytest.mark.parametrize("factory", [client_decorator, server_decorator])
def test_time_limits_on_an_excluded_action_method_raise(factory):
    with pytest.raises(ValueError, match=r"TimedViewSet\.light configures TimeLimits"):
        timed_class(factory(exclude=["light"]), "action")


@pytest.mark.parametrize("factory", [client_decorator, server_decorator])
def test_time_limits_on_the_perform_method_of_an_action_left_out_raise(factory):
    with pytest.raises(ValueError, match=r"TimedViewSet\.perform_list configures TimeLimits"):
        timed_class(factory(include=["light"]), "perform")


def test_time_limits_on_a_deferred_action_are_accepted():
    cls = timed_class(client_decorator(exclude=["list_items"]), "action")
    assert getattr(cls, DEFERRED_ACTIONS_ATTRIBUTE) == {"light"}


def test_class_level_and_default_time_limits_do_not_raise(monkeypatch):
    monkeypatch.setattr(settings, "default_action_configuration", {TimeLimits: TimeLimits(hard=60)})

    @client_decorator(exclude=["list_items"])
    @action_configuration({TimeLimits: TimeLimits(soft=5)})
    class ClassTimedViewSet(ListMixin[Row]):
        __router = APIRouter()

        async def perform_list(self, _context) -> list[Row]:
            return []

        @__router.get("light")
        async def light(self, context: Context) -> Row:  # noqa: ARG002 - injected by name
            return Row(id=1, name="light")

    assert getattr(ClassTimedViewSet, DEFERRED_ACTIONS_ATTRIBUTE) == {"light"}


# ---------------------------------------------------------------------------
# Running: local and deferred actions through HTTP
# ---------------------------------------------------------------------------


def test_a_local_action_runs_in_process_with_one_execute_and_no_transition(monkeypatch, probe):
    app, tasks = worker_app()
    report_class(server_decorator(app, exclude=["list_items", "light", "chain"]))
    client_app = connected_client_app(monkeypatch, tasks)
    cls = report_class(
        celery_viewset_client(
            celery_app=client_app,
            task_prefix="reports",
            redis_client=MagicMock(),
            exclude=["list_items", "light", "chain"],
        )
    )
    probe.seen.clear()

    response = served(cls).get("/reports")

    assert response.json() == [{"id": 1, "name": "Ana"}]
    client_app.send_task.assert_not_called()
    assert probe.seen == [("call", "ReportViewSet", "list_items"), ("execute", "ReportViewSet", "list_items")]


def test_a_deferred_action_runs_in_the_worker(monkeypatch, probe):
    app, tasks = worker_app()
    report_class(server_decorator(app, include=["heavy"]))
    client_app = connected_client_app(monkeypatch, tasks)
    cls = report_class(
        celery_viewset_client(celery_app=client_app, task_prefix="reports", redis_client=MagicMock(), include=["heavy"])
    )
    probe.seen.clear()

    response = served(cls).get("/reports/heavy")

    assert response.json() == {"id": 2, "name": "heavy"}
    assert client_app.send_task.call_args.args[0] == "reports.heavy"
    assert ("transition", "dispatched", "heavy") in probe.seen
    assert [entry for entry in probe.seen if entry[0] == "execute"] == [("execute", "ReportViewSet", "heavy")]


def test_a_local_action_calling_a_deferred_one_dispatches_its_task(monkeypatch, probe):
    app, tasks = worker_app()
    report_class(server_decorator(app, include=["heavy"]))
    client_app = connected_client_app(monkeypatch, tasks)
    cls = report_class(
        celery_viewset_client(celery_app=client_app, task_prefix="reports", redis_client=MagicMock(), include=["heavy"])
    )

    response = served(cls).get("/reports/light")

    assert response.json() == [{"id": 3, "name": "light"}, {"id": 2, "name": "heavy"}]
    assert [call.args[0] for call in client_app.send_task.call_args_list] == ["reports.heavy"]
    executes = [entry for entry in probe.seen if entry[0] == "execute"]
    assert executes == [("execute", "ReportViewSet", "light"), ("execute", "ReportViewSet", "heavy")]


@pytest.mark.usefixtures("probe")
def test_a_deferred_action_calling_local_ones_runs_them_in_the_worker(monkeypatch):
    app, tasks = worker_app()
    report_class(server_decorator(app, include=["chain"]))
    client_app = connected_client_app(monkeypatch, tasks)
    cls = report_class(
        celery_viewset_client(celery_app=client_app, task_prefix="reports", redis_client=MagicMock(), include=["chain"])
    )

    response = served(cls).get("/reports/chain")

    assert response.json() == [{"id": 3, "name": "light"}, {"id": 2, "name": "heavy"}]
    assert [call.args[0] for call in client_app.send_task.call_args_list] == ["reports.chain"]


# ---------------------------------------------------------------------------
# Sub-viewsets
# ---------------------------------------------------------------------------


@pytest.mark.usefixtures("probe")
def test_the_parents_exclude_does_not_reach_a_child(monkeypatch):
    class MembersViewSet(ListMixin[Row]):
        async def perform_list(self, _context) -> list[Row]:
            return [Row(id=5, name="member")]

    app, tasks = worker_app()
    celery_viewset_server(celery_app=app, task_prefix="members", redis_client=MagicMock())(
        type("MembersCeleryViewSet", (MembersViewSet,), {})
    )
    client_app = connected_client_app(monkeypatch, tasks)

    @celery_viewset_client(celery_app=client_app, task_prefix="members", redis_client=MagicMock())
    class MembersCeleryViewSet(MembersViewSet):
        pass

    @celery_viewset_client(
        celery_app=client_app, task_prefix="accounts", redis_client=MagicMock(), exclude=["list_items"]
    )
    class AccountViewSet(ListMixin[Row]):
        __router = ViewSetRouter()
        __router.sub_viewset("members", MembersCeleryViewSet)

        async def perform_list(self, _context) -> list[Row]:
            return []

    assert getattr(MembersCeleryViewSet, DEFERRED_ACTIONS_ATTRIBUTE) == {"list_items"}
    assert DEFERRED_ACTIONS_ATTRIBUTE not in vars(MembersViewSet)

    router = APIRouter()
    route_viewset(router, "/accounts")(AccountViewSet)
    fastapi_app = FastAPI()
    fastapi_app.include_router(router)
    response = TestClient(fastapi_app).get("/accounts/members")

    assert response.json() == [{"id": 5, "name": "member"}]
    assert client_app.send_task.call_args.args[0] == "members.list_items"


# ---------------------------------------------------------------------------
# Subclass of a decorated viewset
# ---------------------------------------------------------------------------


def test_a_subclass_excluding_an_action_of_a_decorated_base_runs_it_in_process(monkeypatch, probe):
    client_app = connected_client_app(monkeypatch, {})
    base = report_class(celery_viewset_client(celery_app=client_app, task_prefix="base", redis_client=MagicMock()))

    @celery_viewset_client(celery_app=client_app, task_prefix="sub", redis_client=MagicMock(), exclude=["heavy"])
    class SubViewSet(base):
        pass

    assert getattr(SubViewSet, DEFERRED_ACTIONS_ATTRIBUTE) == {"list_items", "light", "chain"}
    assert getattr(base, DEFERRED_ACTIONS_ATTRIBUTE) == {"list_items", "heavy", "light", "chain"}
    probe.seen.clear()

    response = served(SubViewSet).get("/reports/heavy")

    assert response.json() == {"id": 2, "name": "heavy"}
    client_app.send_task.assert_not_called()
    assert probe.seen == [("call", "SubViewSet", "heavy"), ("execute", "SubViewSet", "heavy")]


def test_a_subclass_of_a_decorated_base_dispatches_its_own_tasks(monkeypatch):
    app, tasks = worker_app()
    report_class(server_decorator(app, include=["chain"]))
    client_app = connected_client_app(monkeypatch, {"sub.chain": tasks["reports.chain"]})
    base = report_class(celery_viewset_client(celery_app=client_app, task_prefix="base", redis_client=MagicMock()))

    @celery_viewset_client(celery_app=client_app, task_prefix="sub", redis_client=MagicMock(), include=["chain"])
    class SubViewSet(base):
        pass

    response = served(SubViewSet).get("/reports/chain")

    assert response.json() == [{"id": 3, "name": "light"}, {"id": 2, "name": "heavy"}]
    assert [call.args[0] for call in client_app.send_task.call_args_list] == ["sub.chain"]
