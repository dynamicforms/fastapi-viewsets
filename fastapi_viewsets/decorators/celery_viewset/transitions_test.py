"""What the worker's supervisor reports for a call it ends, or never runs, outside the execution."""

import json
import uuid

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from billiard.exceptions import TimeLimitExceeded, WorkerLostError
from fastapi import APIRouter

from ...action_configuration import action_configuration
from ...conf import settings
from ...middleware import ActionTransition, Middleware
from . import server
from .server import celery_viewset_server


class Listener(Middleware):
    def __init__(self, log: list, fails: bool = False):
        self.log = log
        self.fails = fails

    async def __call__(self, _request, _viewset, _context, call_next):
        return await call_next()

    async def executor_started(self):
        self.log.append(("started", None))

    async def on_transition(self, transition: ActionTransition):
        self.log.append(("transition", transition))
        if self.fails:
            raise RuntimeError("listener failed")


class Extra(Listener):
    pass


@pytest.fixture
def log(monkeypatch):
    entries = []
    monkeypatch.setattr(settings, "viewsets_command_middleware", [Listener(entries)])
    monkeypatch.setattr(server, "_transition_loop", None)
    return entries


@pytest.fixture
def redis_client(log):
    client = MagicMock()
    client.rpush.side_effect = lambda _key, value: log.append(("pushed", json.loads(value)))
    return client


@pytest.fixture
def jobs(redis_client, log):
    @action_configuration({Extra(log): None})
    class Jobs:
        __router = APIRouter()

        @__router.post("")
        async def run(self) -> None:
            return None

    celery_app = MagicMock()
    celery_app.task.side_effect = lambda **_kwargs: lambda func: func
    celery_viewset_server(celery_app=celery_app, task_prefix="supervised", redis_client=redis_client)(Jobs)
    return Jobs


def _kwargs() -> dict:
    return {
        "_correlation_id": uuid.uuid4().hex,
        "_result_queue_key": "celery_viewset_results:supervised:h:1",
        "_context": {"user": "u"},
        "_parent": {"viewset_class": "app.Outer", "action": "act", "parent": None},
    }


TASK = SimpleNamespace(name="supervised.run")


def _settled(future) -> None:
    assert future is not None
    future.result(timeout=5)


def _transitions(log) -> list[ActionTransition]:
    return [entry for kind, entry in log if kind == "transition"]


@pytest.mark.parametrize(
    ("exception", "reason", "status"),
    [(TimeLimitExceeded(5), "time_limit", 504), (WorkerLostError("gone"), "lost", 503)],
)
def test_a_supervisor_failure_pushes_interrupted_then_reports_it(jobs, log, exception, reason, status):
    _settled(server._on_task_failure(sender=TASK, exception=exception, kwargs=_kwargs()))

    assert [kind for kind, _ in log] == ["pushed", "started", "transition", "transition"]
    pushed = log[0][1]
    assert pushed["error"]["class"] == "ActionInterruptedError"
    assert pushed["error"]["params"] == {"task": "supervised.run", "reason": reason}
    assert pushed["http_status_code"] == status
    transition = log[2][1]
    assert (transition.kind, transition.viewset_class, transition.action) == ("interrupted", jobs, "run")
    assert transition.context.raw() == {"user": "u"}
    assert transition.context.configuration_for(Listener) is None
    assert transition.parent.viewset_class == "app.Outer"
    assert transition.error.params["reason"] == reason


@pytest.mark.usefixtures("jobs")
def test_a_failure_the_execution_raised_is_left_to_it(log):
    assert server._on_task_failure(sender=TASK, exception=ValueError("x"), kwargs=_kwargs()) is None
    assert log == []


@pytest.mark.usefixtures("jobs")
def test_a_task_outside_the_registered_prefixes_is_ignored(log):
    other = SimpleNamespace(name="elsewhere.run")
    assert server._on_task_failure(sender=other, exception=TimeLimitExceeded(5), kwargs=_kwargs()) is None
    assert server._on_task_failure(sender=TASK, exception=TimeLimitExceeded(5), kwargs={"x": 1}) is None
    assert log == []


@pytest.mark.usefixtures("jobs")
def test_a_call_is_reported_once(log):
    kwargs = _kwargs()
    _settled(server._on_task_failure(sender=TASK, exception=WorkerLostError(), kwargs=kwargs))
    assert server._on_task_revoked(sender=TASK, request=SimpleNamespace(kwargs=kwargs), terminated=True) is None
    assert [kind for kind, _ in log].count("pushed") == 1


@pytest.mark.parametrize(
    ("signal_kwargs", "kind", "reason", "error_class"),
    [
        ({"terminated": True}, "interrupted", "terminated", "ActionInterruptedError"),
        ({"terminated": False}, "not_run", "revoked", "ActionDispatchError"),
        ({"terminated": False, "expired": True}, "not_run", "expired", "ActionDispatchError"),
    ],
)
@pytest.mark.usefixtures("jobs")
def test_a_revoked_call(log, signal_kwargs, kind, reason, error_class):
    request = SimpleNamespace(kwargs=_kwargs())
    _settled(server._on_task_revoked(sender=TASK, request=request, **signal_kwargs))

    pushed = log[0][1]
    assert (pushed["error"]["class"], pushed["error"]["params"]["reason"]) == (error_class, reason)
    assert _transitions(log)[0].kind == kind


class Message:
    def __init__(self, name, body):
        self.headers = {"task": name}
        self.body = body

    def decode(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


def test_an_unknown_task_under_a_registered_prefix_did_not_run(jobs, log):
    message = Message("supervised.missing", [[], _kwargs(), {}])
    _settled(server._on_task_unknown(message=message, name="supervised.missing", id="t1"))

    pushed = log[0][1]
    assert pushed["error"]["params"] == {"task": "supervised.missing", "reason": "unknown_task"}
    transition = _transitions(log)[0]
    assert (transition.kind, transition.viewset_class, transition.action) == ("not_run", jobs, "missing")


@pytest.mark.usefixtures("jobs")
def test_a_rejected_message_did_not_run(log):
    _settled(server._on_task_rejected(message=Message("supervised.run", [[], _kwargs(), {}])))
    assert log[0][1]["error"]["params"]["reason"] == "rejected"


@pytest.mark.usefixtures("jobs")
def test_a_rejected_message_that_cannot_be_decoded_is_not_reported(log):
    assert server._on_task_rejected(message=Message("supervised.run", ValueError("garbled"))) is None
    assert log == []


@pytest.mark.usefixtures("jobs")
def test_an_internal_error_failed(log):
    _settled(server._on_task_internal_error(sender=TASK, kwargs=_kwargs()))
    assert log[0][1]["error"]["params"] == {"task": "supervised.run", "reason": "executor"}
    assert _transitions(log)[0].kind == "failed"


@pytest.mark.usefixtures("jobs")
def test_a_listener_that_raises_changes_nothing(log, monkeypatch):
    monkeypatch.setattr(settings, "viewsets_command_middleware", [Listener(log, fails=True)])
    _settled(server._on_task_internal_error(sender=TASK, kwargs=_kwargs()))
    assert [kind for kind, _ in log] == ["pushed", "started", "transition", "transition"]


@pytest.mark.usefixtures("jobs")
def test_executor_started_runs_once_before_the_first_transition(log):
    _settled(server._on_task_internal_error(sender=TASK, kwargs=_kwargs()))
    _settled(server._on_task_internal_error(sender=TASK, kwargs=_kwargs()))
    assert [kind for kind, _ in log].count("started") == 1


@pytest.mark.usefixtures("jobs")
def test_the_receivers_are_connected_to_celerys_signals():
    from celery import signals

    assert server._receivers_connected
    assert any(receiver is server._on_task_failure for _key, receiver in _live(signals.task_failure))
    assert any(receiver is server._on_task_unknown for _key, receiver in _live(signals.task_unknown))


def _live(celery_signal):
    return [(key, receiver) for key, receiver in celery_signal.receivers]
