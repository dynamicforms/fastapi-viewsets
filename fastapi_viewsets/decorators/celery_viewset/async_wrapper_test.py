"""The async implementation of a `celery_viewset` task, as an async process of Celery with async tasks
runs it: outcomes pushed to the caller when the executor cancels the execution, and
`Middleware.executor_started()` once per loop."""

import asyncio
import json
import threading

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

    tasks = {}
    celery_app = MagicMock()
    celery_app.task.side_effect = lambda name, **_options: lambda fun: tasks.setdefault(name, fun)
    celery_viewset_server(celery_app, "jobs", redis_client=redis_client or MagicMock())(Jobs)
    return tasks[f"jobs.{name}"].__celery_async__


def pushed(redis_client) -> list[dict]:
    return [json.loads(call.args[1]) for call in redis_client.rpush.call_args_list]


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

    redis_client.rpush.side_effect = slow_push
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
