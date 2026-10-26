import asyncio
import json

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient
from muxws.frames import ABSENT
from pydantic import BaseModel

from fastapi_viewsets.decorators import celery_viewset_server, route_viewset
from fastapi_viewsets.decorators.celery_viewset import result_reader
from fastapi_viewsets.decorators.celery_viewset.transport import ActionCodec
from fastapi_viewsets.mux_ws.registry import reset_registry
from fastapi_viewsets.mux_ws.server import process_command
from fastapi_viewsets.mux_ws.server_test import FakeStream


class Item(BaseModel):
    id: int
    name: str


def make_viewset() -> type:
    class ReportViewSet:
        __router = APIRouter()

        @__router.get("report")
        async def report(self) -> dict[str, Any]:
            return {
                "record": Item(id=1, name="one"),
                "items": [Item(id=2, name="two")],
                "nested": {"record": Item(id=3, name="three")},
                "at": datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
                "amount": Decimal("1.50"),
            }

    return ReportViewSet


@pytest.fixture(autouse=True)
def _clean_registry():
    reset_registry()
    yield
    reset_registry()


def rest_result() -> Any:
    app = FastAPI()
    router = APIRouter()
    route_viewset(router, base_path="/reports", pk_field_name="id")(make_viewset())
    app.include_router(router)
    return TestClient(app).get("/reports/report").json()


async def muxws_result() -> Any:
    app = FastAPI()
    router = APIRouter()
    route_viewset(router, base_path="/reports", pk_field_name="id")(make_viewset())
    app.include_router(router)
    stream = FakeStream({":method": "GET", ":path": "/reports/report"})
    await process_command(ABSENT, stream, app=app)
    assert stream.status == 200
    return stream.payload


async def celery_result() -> Any:
    """The worker runs the endpoint and pushes its result; the FastAPI process's result reader decodes it."""
    celery_app = MagicMock()
    redis_mock = MagicMock()
    registered_tasks = {}

    def mock_task(name, **_kwargs):
        def deck(func):
            registered_tasks[name] = func
            return func

        return deck

    celery_app.task.side_effect = mock_task
    server = celery_viewset_server(celery_app=celery_app, task_prefix="reports", redis_client=redis_mock)
    viewset = server(make_viewset())

    correlation_id = "parity"
    queue_key = "celery_viewset_results:reports"
    await asyncio.to_thread(
        registered_tasks["reports.report"], _correlation_id=correlation_id, _result_queue_key=queue_key
    )
    pushed = redis_mock.rpush.call_args[0][1]

    reader_redis = AsyncMock()
    reader_redis.lpop.side_effect = [pushed.encode(), None, None]
    future = asyncio.get_running_loop().create_future()
    result_reader.register_future(correlation_id, future)
    task = asyncio.create_task(result_reader.result_reader_loop(reader_redis, queue_key, poll_interval=0.01))
    try:
        return ActionCodec(viewset, viewset.report).decode_result(await asyncio.wait_for(future, timeout=2.0))
    finally:
        task.cancel()


@pytest.mark.asyncio
async def test_a_nested_result_is_the_same_over_rest_muxws_and_celery():
    expected = {
        "record": {"id": 1, "name": "one"},
        "items": [{"id": 2, "name": "two"}],
        "nested": {"record": {"id": 3, "name": "three"}},
        "at": "2026-01-02T03:04:05Z",
        "amount": "1.50",
    }
    assert rest_result() == expected
    assert await muxws_result() == expected
    assert json.loads(json.dumps(await celery_result())) == expected
