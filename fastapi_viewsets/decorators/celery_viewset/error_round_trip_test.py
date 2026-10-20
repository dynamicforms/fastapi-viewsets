import json

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest

from fastapi import APIRouter, FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import BaseModel

from ...exceptions import DfViewSetError, NotFoundError
from ..route_viewset import route_viewset
from . import celery_viewset_client, celery_viewset_server, result_reader


class Item(BaseModel):
    id: int
    name: str


class _PushingRedis:
    """The worker's `redis_client`: holds the payloads the worker pushes."""

    def __init__(self):
        self.pushed: list[str] = []

    def rpush(self, _key: str, data: str) -> None:
        self.pushed.append(data)

    def expire(self, _key: str, _ttl: int) -> None:
        pass


class _Executor:
    """celery_viewset_client and celery_viewset_server in one process: the client's send_task runs the
    server's task, and the payload the task pushed resolves the client's future as the result reader
    resolves it, after `rewrite` (the payload as a worker of another version writes it)."""

    def __init__(self, monkeypatch, rewrite: Callable[[dict], dict] = lambda payload: payload):
        self.tasks = {}
        self.futures = {}
        self.redis = _PushingRedis()
        self.rewrite = rewrite
        self.server_app = MagicMock()
        self.server_app.task.side_effect = lambda name, **_kwargs: lambda func: self.tasks.setdefault(name, func)
        self.client_app = MagicMock()
        self.client_app.send_task.side_effect = self.send_task
        monkeypatch.setattr(result_reader, "register_future", self.register)

    def register(self, correlation_id, future, _queue_key=None):
        self.futures[correlation_id] = future

    def send_task(self, name, args, kwargs, **_options):
        future = self.futures[kwargs["_correlation_id"]]
        try:
            self.tasks[name](*args, **kwargs)
        except Exception:  # noqa: S110 - the task pushed its error before it re-raised it for Celery
            pass
        payload = self.rewrite(json.loads(self.redis.pushed.pop()))
        future.get_loop().call_soon_threadsafe(result_reader._resolve, future, payload)


def _client_for(monkeypatch, error: Exception, rewrite: Callable[[dict], dict] = lambda payload: payload):
    executor = _Executor(monkeypatch, rewrite)

    class Base:
        __router = APIRouter()

        @__router.post("")
        async def create(self, item: Item) -> Item:  # noqa: ARG002 - fails before reading it
            raise error

    celery_viewset_server(celery_app=executor.server_app, task_prefix="items", redis_client=executor.redis)(
        type("Worker", (Base,), {})
    )
    client = celery_viewset_client(celery_app=executor.client_app, task_prefix="items", redis_client=MagicMock())(
        type("Client", (Base,), {})
    )
    router = APIRouter()
    route_viewset(router, "/items")(client)
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


_FIELD_ERRORS = [{"type": "int_parsing", "loc": ("query", "year__in", 1), "msg": "not an int", "input": "abc"}]


def _body(detail, code, params=None, fields=None):
    return {"detail": detail, "detail_code": code, "detail_params": params or {}, "field_errors": fields or {}}


@pytest.mark.parametrize(
    ("error", "status", "body"),
    [
        (
            RequestValidationError(_FIELD_ERRORS),
            422,
            _body(
                "The request failed validation",
                "fastapi-viewsets.validation_failed",
                fields={"year__in.1": [{"code": "pydantic.int_parsing", "params": {}, "detail": "not an int"}]},
            ),
        ),
        (
            HTTPException(409, "already there"),
            409,
            _body("already there", "fastapi-viewsets.http_error", {"status": 409}),
        ),
        (NotFoundError(3), 404, _body("Item with pk 3 not found", "fastapi-viewsets.not_found", {"pk": 3})),
        (DfViewSetError(402, "short", "app.short", {"n": 1}), 402, _body("short", "app.short", {"n": 1})),
        (
            RuntimeError("secret detail of the failure"),
            500,
            _body("items.create failed", "fastapi-viewsets.action_failed", {"task": "items.create", "reason": "error"}),
        ),
    ],
)
def test_an_error_of_the_worker_is_answered_with_the_body_of_the_fastapi_process(monkeypatch, error, status, body):
    response = _client_for(monkeypatch, error).post("/items", json={"id": 1, "name": "a"})
    assert (response.status_code, response.json()) == (status, body)
    assert "secret" not in response.text


def test_a_payload_a_0_11_worker_pushed_is_answered_with_the_error_body(monkeypatch):
    def as_0_11(payload: dict) -> dict:
        payload["error"].pop("class")
        return payload

    response = _client_for(monkeypatch, RequestValidationError(_FIELD_ERRORS), as_0_11).post(
        "/items", json={"id": 1, "name": "a"}
    )
    assert response.status_code == 422
    assert response.json()["detail_code"] == "fastapi-viewsets.http_error"
    assert response.json()["detail_params"]["status"] == 422
