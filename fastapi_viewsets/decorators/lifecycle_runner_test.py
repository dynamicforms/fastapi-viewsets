from contextlib import asynccontextmanager

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from ..context import Context
from .lifecycle_runner import CELERY_DISPATCH_ATTRIBUTE, lifecycle_runner, set_execution_hook
from .route_viewset import route_viewset


@pytest.fixture
def events():
    recorded = []

    @asynccontextmanager
    async def hook(execution):
        recorded.append(("enter", execution))
        try:
            yield
        except Exception as error:
            recorded.append(("error", error))
            raise
        recorded.append(("exit", None))

    set_execution_hook(hook)
    yield recorded
    set_execution_hook(None)


def test_the_hook_wraps_an_in_process_action(events):
    router = APIRouter()

    @route_viewset(router, "/things")
    class ThingViewSet:
        __router = APIRouter()

        @__router.post("")
        async def create(self) -> dict:
            events.append(("action", None))
            return {"ok": True}

    app = FastAPI()
    app.include_router(router)
    assert TestClient(app).post("/things").json() == {"ok": True}

    assert [kind for kind, _ in events] == ["enter", "action", "exit"]
    execution = events[0][1]
    assert (execution.viewset_class, execution.action) == (ThingViewSet, "create")
    assert isinstance(execution.context, Context)
    assert execution.request.url.path == "/things"


@pytest.mark.asyncio
async def test_the_hook_sees_the_exception_of_the_action(events):
    class Failing:
        async def act(self):
            raise ValueError("no")

    with pytest.raises(ValueError, match="no"):
        await lifecycle_runner(Failing.act, Failing(), Failing)
    assert [kind for kind, _ in events] == ["enter", "error"]
    assert isinstance(events[1][1], ValueError)


@pytest.mark.asyncio
async def test_the_hook_runs_in_the_worker_path_with_the_context_it_was_given(events):
    class Worker:
        async def act(self, context):
            return await context["token"]

    context = Context({"token": "t"})
    assert await lifecycle_runner(Worker.act, Worker(), Worker, context=context) == "t"
    execution = events[0][1]
    assert (execution.action, execution.context, execution.request) == ("act", context, None)


@pytest.mark.asyncio
async def test_a_celery_dispatch_stub_is_not_wrapped(events):
    class Client:
        async def act(self):
            return "dispatched"

    setattr(Client.act, CELERY_DISPATCH_ATTRIBUTE, True)
    assert await lifecycle_runner(Client.act, Client(), Client) == "dispatched"
    assert events == []
