"""A `celery_viewset` mounted as a sub-viewset: the parent's and its own checks run in the web
process, the action executes in the worker under `Middleware.execute()`."""

import asyncio

from unittest.mock import MagicMock

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ...conf import settings
from ...context import to_jsonable
from ...middleware import Middleware
from ...mixins import ListMixin
from ...mux_ws.registry import reset_registry
from .. import route_viewset, ViewSetRouter
from . import celery_viewset_client, celery_viewset_server, result_reader


class Account(BaseModel):
    id: int
    name: str


class MemberRow(BaseModel):
    id: int
    name: str


class Probe(Middleware):
    def __init__(self):
        self.seen: list[tuple[str, str, str | None]] = []

    async def depends(self, _request, cls, context) -> None:
        self.seen.append(("depends", cls.__name__, context.action_name))

    async def __call__(self, _request, viewset, context, call_next):
        self.seen.append(("call", type(viewset).__name__, context.action_name))
        return await call_next()

    async def execute(self, execution, call_next):
        self.seen.append(("execute", execution.viewset_class.__name__, execution.action))
        return await call_next()


@pytest.fixture(autouse=True)
def _reset():
    saved = settings.viewsets_command_middleware
    reset_registry()
    yield
    settings.viewsets_command_middleware = saved
    reset_registry()


def members_class(decorate):
    @decorate
    class MembersViewSet(ListMixin[MemberRow]):
        async def perform_list(self, _context) -> list[MemberRow]:
            return [MemberRow(id=1, name="Ana")]

    return MembersViewSet


def test_a_celery_child_passes_both_gates_and_executes_in_the_worker(monkeypatch):
    probe = Probe()
    settings.viewsets_command_middleware = [probe]

    worker_app, tasks = MagicMock(), {}

    def register_task(name, **_options):
        def decorator(function):
            tasks[name] = function
            return function

        return decorator

    worker_app.task.side_effect = register_task
    members_class(celery_viewset_server(celery_app=worker_app, task_prefix="members", redis_client=MagicMock()))

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

    client_app = MagicMock()
    client_app.send_task.side_effect = send_task
    members = members_class(
        celery_viewset_client(celery_app=client_app, task_prefix="members", redis_client=MagicMock())
    )
    router = APIRouter()

    @route_viewset(router, "/accounts")
    class AccountViewSet(ListMixin[Account]):
        __router = ViewSetRouter()
        __router.sub_viewset("members", members)

        async def perform_list(self, _context) -> list[Account]:
            return []

    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).get("/accounts/members")
    assert response.json() == [{"id": 1, "name": "Ana"}]
    assert client_app.send_task.call_args.args[0] == "members.list_items"
    assert probe.seen == [
        ("depends", "AccountViewSet", "members"),
        ("depends", "MembersViewSet", "list_items"),
        ("call", "MembersViewSet", "list_items"),
        ("execute", "MembersViewSet", "list_items"),
    ]
