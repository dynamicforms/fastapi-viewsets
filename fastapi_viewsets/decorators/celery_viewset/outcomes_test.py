"""What a `celery_viewset_server` task pushes and raises when its execution does not return."""

import asyncio
import json
import os
import signal
import threading
import time

from unittest.mock import MagicMock

import pytest

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ...conf import settings
from ...exceptions import ActionFailedError, ActionInterruptedError, NotFoundError
from ...middleware import Middleware
from . import server
from .server import celery_viewset_server


class Recorder(Middleware):
    def __init__(self, events: list):
        self.events = events

    async def __call__(self, _request, _viewset, _context, call_next):
        return await call_next()

    async def execute(self, _execution, call_next):
        try:
            return await call_next()
        except BaseException as error:
            self.events.append(("execute", error))
            raise


@pytest.fixture
def events(monkeypatch):
    recorded = []
    monkeypatch.setattr(settings, "viewsets_command_middleware", [Recorder(recorded)])
    return recorded


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch):
    monkeypatch.setattr(server, "_started_pid", None)


def _worker(cls, redis_client=None, prefix="jobs"):
    """The tasks `celery_viewset_server` registers for `cls`, by name, and the redis client they push to."""
    tasks = {}
    celery_app = MagicMock()
    celery_app.task.side_effect = lambda name, **_kwargs: lambda func: tasks.setdefault(name, func)
    redis_client = redis_client or MagicMock()
    celery_viewset_server(
        celery_app=celery_app, task_prefix=prefix, lifecycle="instance-key", redis_client=redis_client
    )(cls)
    return tasks, redis_client


def _pushed(redis_client) -> dict:
    return json.loads(redis_client.rpush.call_args[0][1])


CALL = {"_correlation_id": "c1", "_result_queue_key": "celery_viewset_results:jobs:h:1"}


@pytest.fixture
def soft_limit_signal():
    """Billiard's soft time limit handler on SIGUSR1, as a prefork pool child has it; `send(delay)`
    signals this process after `delay` seconds."""
    from billiard.pool import soft_timeout_sighandler

    previous = signal.signal(signal.SIGUSR1, soft_timeout_sighandler)
    timers = []

    def send(delay: float) -> None:
        timer = threading.Timer(delay, os.kill, (os.getpid(), signal.SIGUSR1))
        timers.append(timer)
        timer.start()

    yield send
    for timer in timers:
        timer.cancel()
    signal.signal(signal.SIGUSR1, previous)


def _state_recording_viewset(body, saved: list):
    class Jobs:
        __router = APIRouter()

        async def save_state(self):
            saved.append(True)

        @__router.post("")
        async def run(self) -> None:
            return await body()

    return Jobs


def test_a_soft_time_limit_in_synchronous_code_raises_action_interrupted_there(events, soft_limit_signal):
    seen, saved = [], []

    async def body():
        try:
            time.sleep(5)  # noqa: ASYNC251 - synchronous code is the case under test
        except BaseException as error:
            seen.append(error)
            raise

    tasks, redis_client = _worker(_state_recording_viewset(body, saved))
    soft_limit_signal(0.05)
    with pytest.raises(ActionInterruptedError) as raised:
        tasks["jobs.run"](**CALL)

    assert seen == [raised.value]
    assert events == [("execute", raised.value)]
    assert saved == [True]
    assert raised.value.status_code == 504
    payload = _pushed(redis_client)
    assert payload["error"]["class"] == "ActionInterruptedError"
    assert payload["error"]["params"] == {"task": "jobs.run", "reason": "time_limit"}


def test_a_soft_time_limit_while_suspended_cancels_the_action_and_interrupts_execute(events, soft_limit_signal):
    seen, saved = [], []

    async def body():
        try:
            await asyncio.sleep(5)
        except BaseException as error:
            seen.append(error)
            raise

    tasks, redis_client = _worker(_state_recording_viewset(body, saved))
    soft_limit_signal(0.05)
    with pytest.raises(ActionInterruptedError) as raised:
        tasks["jobs.run"](**CALL)

    assert isinstance(seen[0], asyncio.CancelledError)
    assert events == [("execute", raised.value)]
    assert saved == [True]
    assert _pushed(redis_client)["error"]["class"] == "ActionInterruptedError"


@pytest.mark.usefixtures("soft_limit_signal")
def test_the_soft_limit_handler_is_restored_after_the_execution():
    from billiard.pool import soft_timeout_sighandler

    async def body():
        return None

    tasks, _redis = _worker(_state_recording_viewset(body, []))
    tasks["jobs.run"](**CALL)
    assert signal.getsignal(signal.SIGUSR1) is soft_timeout_sighandler


@pytest.mark.parametrize("error_class", ["Retry", "Ignore", "Reject"])
def test_a_celery_exception_the_action_raises_is_action_failed(events, error_class):
    import celery.exceptions

    async def body():
        raise getattr(celery.exceptions, error_class)()

    tasks, redis_client = _worker(_state_recording_viewset(body, []))
    with pytest.raises(ActionFailedError) as raised:
        tasks["jobs.run"](**CALL)

    assert isinstance(raised.value.__cause__, getattr(celery.exceptions, error_class))
    assert events == [("execute", raised.value)]
    assert _pushed(redis_client)["error"]["params"] == {"task": "jobs.run", "reason": "error"}


class Body(BaseModel):
    name: str


def test_arguments_that_do_not_validate_are_action_failed():
    class Jobs:
        __router = APIRouter()

        @__router.post("")
        async def run(self, data: Body) -> Body:
            return data

    tasks, redis_client = _worker(Jobs)
    with pytest.raises(ActionFailedError):
        tasks["jobs.run"](data={"name": 1, "extra": {}}, **CALL)
    assert _pushed(redis_client)["error"]["code"] == "action_failed"


def test_a_base_exception_is_pushed_and_raised_as_action_failed():
    class Stop(BaseException):
        pass

    async def body():
        raise Stop

    tasks, redis_client = _worker(_state_recording_viewset(body, []))
    with pytest.raises(ActionFailedError):
        tasks["jobs.run"](**CALL)
    assert _pushed(redis_client)["error"]["class"] == "ActionFailedError"


def test_a_process_exit_is_pushed_as_terminated_and_raised_as_it_is():
    async def body():
        raise SystemExit(1)

    tasks, redis_client = _worker(_state_recording_viewset(body, []))
    with pytest.raises(SystemExit):
        tasks["jobs.run"](**CALL)
    assert _pushed(redis_client)["error"]["params"] == {"task": "jobs.run", "reason": "terminated"}


def test_an_http_exception_is_the_answer_and_a_package_error_keeps_its_class():
    async def body():
        raise NotFoundError(7)

    tasks, redis_client = _worker(_state_recording_viewset(body, []))
    assert tasks["jobs.run"](**CALL) is None
    error = _pushed(redis_client)["error"]
    assert (error["class"], error["code"], error["params"]) == ("NotFoundError", "not_found", {"pk": 7})


def test_a_result_that_cannot_be_serialized_is_action_failed():
    async def body():
        return object()

    tasks, redis_client = _worker(_state_recording_viewset(body, []))
    with pytest.raises(ActionFailedError):
        tasks["jobs.run"](**CALL)
    assert _pushed(redis_client)["error"]["params"] == {"task": "jobs.run", "reason": "result"}


# ---------------------------------------------------------------------------
# executor_started
# ---------------------------------------------------------------------------


class Starting(Middleware):
    def __init__(self, failures: int = 0):
        self.failures = failures
        self.loops = []

    async def __call__(self, _request, _viewset, _context, call_next):
        return await call_next()

    async def executor_started(self):
        self.loops.append(asyncio.get_running_loop())
        if self.failures:
            self.failures -= 1
            raise RuntimeError("not ready")


def test_executor_started_runs_once_per_process_on_the_loop_of_the_actions(monkeypatch):
    starting = Starting()
    monkeypatch.setattr(settings, "viewsets_command_middleware", [starting])
    loops = []

    async def body():
        loops.append(asyncio.get_running_loop())

    tasks, _redis = _worker(_state_recording_viewset(body, []))
    tasks["jobs.run"](**CALL)
    tasks["jobs.run"](**CALL)
    assert starting.loops == loops[:1]
    assert len(loops) == 2


def test_executor_started_that_fails_fails_the_execution_and_runs_again(monkeypatch):
    starting = Starting(failures=1)
    monkeypatch.setattr(settings, "viewsets_command_middleware", [starting])
    ran = []

    async def body():
        ran.append(True)

    tasks, redis_client = _worker(_state_recording_viewset(body, []))
    with pytest.raises(ActionFailedError):
        tasks["jobs.run"](**CALL)
    assert _pushed(redis_client)["error"]["params"] == {"task": "jobs.run", "reason": "executor"}
    assert ran == []

    tasks["jobs.run"](**CALL)
    assert ran == [True]
    assert len(starting.loops) == 2


def test_an_http_exception_without_a_code_is_pushed_with_its_status():
    async def body():
        raise HTTPException(status_code=409, detail="busy", headers={"Retry-After": "1"})

    tasks, redis_client = _worker(_state_recording_viewset(body, []))
    tasks["jobs.run"](**CALL)
    payload = _pushed(redis_client)
    assert payload["error"] == {"status_code": 409, "detail": "busy", "headers": {"Retry-After": "1"}}
    assert (payload["http_status_code"], payload["http_detail"]) == (409, "busy")
