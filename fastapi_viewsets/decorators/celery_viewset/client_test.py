import asyncio

from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from pydantic import BaseModel

from fastapi_viewsets.context import SerializableObject
from fastapi_viewsets.decorators import (
    celery_viewset_client,
)
from fastapi_viewsets.exceptions import ResultReaderStoppedError, ResultTimeoutError
from fastapi_viewsets.mixins import ListMixin


class Item(BaseModel):
    id: int
    name: str


class ItemWithDate(BaseModel):
    id: int
    created: date
    updated: datetime


# ---------------------------------------------------------------------------
# celery_viewset_client tests
# ---------------------------------------------------------------------------


def test_celery_viewset_client_patches_methods():
    """Client decorator replaces viewset methods with async wrappers."""
    celery_app = MagicMock()
    redis_mock = MagicMock()

    @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self) -> list[Item]:
            return [Item(id=1, name="test")]

    instance = ItemViewSet()
    assert asyncio.iscoroutinefunction(instance.list_items)


def test_celery_viewset_client_sends_task():
    """Client sends Celery task with correlation_id when method is called."""
    from fastapi_viewsets.decorators.celery_viewset import result_reader

    celery_app = MagicMock()
    redis_mock = MagicMock()

    original_register = result_reader.register_future

    def mock_register(_correlation_id, future, _queue_key=None):
        # Immediately resolve the future with a mock result
        asyncio.get_event_loop().call_soon(lambda: future.set_result(["mocked"]))

    result_reader.register_future = mock_register
    try:

        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return [Item(id=1, name="test")]

        instance = ItemViewSet()
        # Client patches the mixin endpoint method name ("list"), not "perform_list"
        result = asyncio.run(instance.list_items())
        assert result == ["mocked"]
        celery_app.send_task.assert_called_once()
        call_kwargs = celery_app.send_task.call_args
        assert call_kwargs[0][0] == "items.list_items"
    finally:
        result_reader.register_future = original_register


def test_celery_viewset_client_sends_date_fields_as_json_safe_strings():
    """BaseModel kwargs with date/datetime fields must serialize to JSON-safe strings for Celery/Kombu."""
    import json

    from fastapi_viewsets.decorators.celery_viewset import result_reader
    from fastapi_viewsets.mixins import CreateMixin

    celery_app = MagicMock()
    redis_mock = MagicMock()

    original_register = result_reader.register_future

    def mock_register(_correlation_id, future, _queue_key=None):
        asyncio.get_event_loop().call_soon(lambda: future.set_result(None))

    result_reader.register_future = mock_register
    try:

        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(CreateMixin[int, ItemWithDate]):
            async def perform_create(self, data: ItemWithDate) -> ItemWithDate:
                return data

        instance = ItemViewSet()
        payload = ItemWithDate(id=1, created=date(2026, 8, 1), updated=datetime(2026, 8, 1, 12, 30))
        asyncio.run(instance.create(data=payload))

        sent_kwargs = celery_app.send_task.call_args.kwargs["kwargs"]
        assert sent_kwargs["data"] == {"id": 1, "created": "2026-08-01", "updated": "2026-08-01T12:30:00"}
        # Must not raise - this is what Kombu's JSON encoder does under the hood.
        json.dumps(sent_kwargs)
    finally:
        result_reader.register_future = original_register


def test_celery_viewset_client_serializes_positional_basemodel_args():
    """BaseModel values passed positionally (not as kwargs) must also be serialized to JSON-safe structures."""
    import json

    from fastapi_viewsets.decorators.celery_viewset import result_reader
    from fastapi_viewsets.mixins import CreateMixin

    celery_app = MagicMock()
    redis_mock = MagicMock()

    original_register = result_reader.register_future

    def mock_register(_correlation_id, future, _queue_key=None):
        asyncio.get_event_loop().call_soon(lambda: future.set_result(None))

    result_reader.register_future = mock_register
    try:

        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(CreateMixin[int, ItemWithDate]):
            async def perform_create(self, data: ItemWithDate) -> ItemWithDate:
                return data

        instance = ItemViewSet()
        payload = ItemWithDate(id=1, created=date(2026, 8, 1), updated=datetime(2026, 8, 1, 12, 30))
        # Call with a positional arg rather than a keyword arg.
        asyncio.run(instance.create(payload))

        sent_args = celery_app.send_task.call_args.kwargs["args"]
        assert sent_args == [{"id": 1, "created": "2026-08-01", "updated": "2026-08-01T12:30:00"}]
        # Must not raise - this is what Kombu's JSON encoder does under the hood.
        json.dumps(sent_args)
    finally:
        result_reader.register_future = original_register


def test_serialize_value_recurses_into_list_and_dict_of_basemodel():
    """list/dict values containing BaseModel instances must be serialized element-wise, not passed through raw."""
    import json

    from fastapi_viewsets.decorators.celery_viewset.client import _serialize_value

    items = [Item(id=1, name="a"), Item(id=2, name="b")]
    serialized_list = asyncio.run(_serialize_value(items))
    assert serialized_list == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    json.dumps(serialized_list)

    mapping = {"first": Item(id=1, name="a"), "second": Item(id=2, name="b")}
    serialized_dict = asyncio.run(_serialize_value(mapping))
    assert serialized_dict == {"first": {"id": 1, "name": "a"}, "second": {"id": 2, "name": "b"}}
    json.dumps(serialized_dict)


def test_serialize_value_partial_states_only_the_fields_a_model_has_set():
    from fastapi_viewsets.decorators.celery_viewset.client import _serialize_value

    class Described(BaseModel):
        id: int = 0
        name: str
        description: str | None = None

    stated = Described.model_construct(_fields_set={"name"}, name="x")
    assert asyncio.run(_serialize_value(stated, partial=True)) == {"name": "x"}
    assert asyncio.run(_serialize_value({1: stated}, partial=True)) == {1: {"name": "x"}}
    assert asyncio.run(_serialize_value(stated)) == {"id": 0, "name": "x", "description": None}


class _TaggedArg(SerializableObject):
    """Module-level (importable) SerializableObject fixture - see SerializableObject's own
    docstring on why a locally-defined subclass can't round-trip through the type tag."""

    def __serialize__(self):
        return self.value

    @classmethod
    def __deserialize__(cls, data):
        return cls(data)


def test_serialize_value_tags_a_serializable_object_argument():
    """A call argument that is itself a SerializableObject (not wrapped in Context) must be
    tagged too, via the same fastapi_viewsets.context.to_jsonable() server.py's _to_jsonable and
    ViewSetResult's own (de)serialization already use."""
    from fastapi_viewsets.decorators.celery_viewset.client import _serialize_value

    serialized = asyncio.run(_serialize_value(_TaggedArg(7)))
    assert serialized == {"__fpv_type__": f"{_TaggedArg.__module__}.{_TaggedArg.__qualname__}", "__fpv_value__": 7}


def test_celery_viewset_client_sends_task_with_ignore_result():
    """send_task must be called with ignore_result=True explicitly.

    Celery's own send_task() defaults ignore_result to False regardless of how the target task
    was registered (celery_viewset_server sets ignore_result=True) - a request-level value always
    overrides the task's own, so the client must repeat it here or the worker still tries to
    JSON-encode the raw return value into its result backend.
    """
    from fastapi_viewsets.decorators.celery_viewset import result_reader

    celery_app = MagicMock()
    redis_mock = MagicMock()

    original_register = result_reader.register_future

    def mock_register(_correlation_id, future, _queue_key=None):
        asyncio.get_event_loop().call_soon(lambda: future.set_result(["mocked"]))

    result_reader.register_future = mock_register
    try:

        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return [Item(id=1, name="test")]

        instance = ItemViewSet()
        asyncio.run(instance.list_items())

        assert celery_app.send_task.call_args.kwargs["ignore_result"] is True
    finally:
        result_reader.register_future = original_register


def test_celery_viewset_client_unregisters_future_on_send_task_error():
    """Client unregisters future when send_task raises an exception."""
    from fastapi_viewsets.decorators.celery_viewset import result_reader

    celery_app = MagicMock()
    celery_app.send_task.side_effect = RuntimeError("Celery broker unavailable")
    redis_mock = MagicMock()

    @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self) -> list[Item]:
            return []

    instance = ItemViewSet()

    async def run():
        with pytest.raises(RuntimeError, match="Celery broker unavailable"):
            await instance.list_items()
        # Future should be unregistered after error
        assert len(result_reader._pending_futures) == 0

    asyncio.run(run())


def test_celery_viewset_client_sends_this_process_result_queue_key(monkeypatch):
    """The task names the result list of the process that dispatches it, read at dispatch time."""
    from fastapi_viewsets.decorators.celery_viewset import result_reader

    celery_app = MagicMock()
    redis_mock = MagicMock()

    @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self) -> list[Item]:
            return []

    monkeypatch.setattr(result_reader.os, "getpid", lambda: 4242)
    monkeypatch.setattr(
        result_reader,
        "register_future",
        lambda _id, future, _queue_key=None: future.get_loop().call_soon(future.set_result, []),
    )
    asyncio.run(ItemViewSet().list_items())

    sent_key = celery_app.send_task.call_args.kwargs["kwargs"]["_result_queue_key"]
    assert sent_key == result_reader.get_process_queue_key("celery_viewset_results:items")
    assert sent_key.endswith(":4242")


def test_celery_viewset_client_times_out_with_504():
    """A result that does not arrive within result_timeout raises ResultTimeoutError and drops the pending future."""
    from fastapi_viewsets.decorators.celery_viewset import result_reader

    celery_app = MagicMock()
    redis_mock = MagicMock()

    @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock, result_timeout=0.01)
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self) -> list[Item]:
            return []

    async def run():
        with pytest.raises(ResultTimeoutError) as exc_info:
            await ItemViewSet().list_items()
        assert exc_info.value.status_code == 504
        assert exc_info.value.params == {"task": "items.list_items", "timeout": 0.01}
        assert len(result_reader._pending_futures) == 0

    asyncio.run(run())


def test_celery_viewset_client_cancelled_call_drops_the_pending_future():
    """A call cancelled while it awaits its result leaves no pending future."""
    from fastapi_viewsets.decorators.celery_viewset import result_reader

    celery_app = MagicMock()
    redis_mock = MagicMock()

    @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self) -> list[Item]:
            return []

    async def run():
        call = asyncio.create_task(ItemViewSet().list_items())
        while not celery_app.send_task.called:
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.01)
        assert len(result_reader._pending_futures) == 1
        call.cancel()
        with pytest.raises(asyncio.CancelledError):
            await call
        assert len(result_reader._pending_futures) == 0

    asyncio.run(run())


def test_celery_viewset_client_sends_the_task_outside_the_event_loop_thread():
    """send_task runs in a worker thread, so a blocking broker publish does not stall the event loop."""
    import threading

    from fastapi_viewsets.decorators.celery_viewset import result_reader

    celery_app = MagicMock()
    redis_mock = MagicMock()
    send_threads = []
    celery_app.send_task.side_effect = lambda *_args, **_kwargs: send_threads.append(threading.get_ident())

    @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self) -> list[Item]:
            return []

    original_register = result_reader.register_future

    def mock_register(_correlation_id, future, _queue_key=None):
        future.get_loop().call_soon(future.set_result, [])

    result_reader.register_future = mock_register
    try:
        asyncio.run(ItemViewSet().list_items())
    finally:
        result_reader.register_future = original_register

    assert len(send_threads) == 1
    assert send_threads[0] != threading.get_ident()


def test_celery_viewset_client_fails_with_503_when_the_result_reader_stops():
    """A call pending when its queue_key's reader stops raises ResultReaderStoppedError."""
    from fastapi_viewsets.decorators.celery_viewset import result_reader

    celery_app = MagicMock()
    redis_mock = MagicMock()

    async def lpop(_key):
        return None

    redis_mock.lpop = lpop

    @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self) -> list[Item]:
            return []

    async def run():
        queue_key = "celery_viewset_results:items"
        await result_reader.start_result_reader(redis_mock, queue_key)
        call = asyncio.create_task(ItemViewSet().list_items())
        while not celery_app.send_task.called:
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.01)
        await result_reader.stop_result_reader(queue_key)
        with pytest.raises(ResultReaderStoppedError) as exc_info:
            await call
        assert exc_info.value.status_code == 503
        assert exc_info.value.params == {"queue_key": queue_key}
        assert len(result_reader._pending_futures) == 0

    asyncio.run(run())


def test_celery_viewset_client_metadata():
    """Client decorator sets __celery_viewset_metadata__ on the class."""
    celery_app = MagicMock()
    redis_mock = MagicMock()

    @celery_viewset_client(celery_app=celery_app, task_prefix="myprefix", redis_client=redis_mock)
    class ItemViewSet(ListMixin[Item]):
        async def perform_list(self) -> list[Item]:
            return []

    meta = ItemViewSet.__celery_viewset_metadata__
    assert meta["task_prefix"] == "myprefix"
    assert meta["mode"] == "client"
    assert meta["celery_app"] is celery_app
    assert meta["redis_client"] is redis_mock


# ---------------------------------------------------------------------------
# celery dispatch hook
# ---------------------------------------------------------------------------


def test_celery_dispatch_hook_none_preserves_existing_behavior():
    """With no hook registered, send_task kwargs are unchanged."""
    from fastapi_viewsets.decorators.celery_viewset import client, result_reader

    celery_app = MagicMock()
    redis_mock = MagicMock()

    assert client._celery_dispatch_hook is None

    original_register = result_reader.register_future

    def mock_register(_correlation_id, future, _queue_key=None):
        asyncio.get_event_loop().call_soon(lambda: future.set_result(["mocked"]))

    result_reader.register_future = mock_register
    try:

        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return [Item(id=1, name="test")]

        instance = ItemViewSet()
        asyncio.run(instance.list_items())

        sent_kwargs = celery_app.send_task.call_args.kwargs["kwargs"]
        assert set(sent_kwargs) == {"_correlation_id", "_result_queue_key"}
    finally:
        result_reader.register_future = original_register


def test_celery_dispatch_hook_awaited_before_send_task():
    """The hook is awaited, and its result merged into kwargs, before send_task is called."""
    from fastapi_viewsets.decorators.celery_viewset import result_reader
    from fastapi_viewsets.decorators.celery_viewset.client import set_celery_dispatch_hook

    celery_app = MagicMock()
    redis_mock = MagicMock()

    call_order = []
    received_kwargs = {}
    celery_app.send_task.side_effect = lambda *_args, **_kwargs: call_order.append("send_task")

    async def hook(kwargs):
        received_kwargs.update(kwargs)
        call_order.append("hook")
        return {"_extra_field": "abc"}

    original_register = result_reader.register_future

    def mock_register(_correlation_id, future, _queue_key=None):
        asyncio.get_event_loop().call_soon(lambda: future.set_result(None))

    result_reader.register_future = mock_register
    set_celery_dispatch_hook(hook)
    try:

        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return []

        instance = ItemViewSet()
        asyncio.run(instance.list_items())

        assert call_order == ["hook", "send_task"]
        sent_kwargs = celery_app.send_task.call_args.kwargs["kwargs"]
        assert sent_kwargs["_extra_field"] == "abc"
        assert received_kwargs == {}
    finally:
        set_celery_dispatch_hook(None)
        result_reader.register_future = original_register


def test_celery_dispatch_hook_receives_call_kwargs():
    """The hook receives the action's own kwargs, e.g. a Context put there by a context processor."""
    from fastapi_viewsets.decorators.celery_viewset import result_reader
    from fastapi_viewsets.decorators.celery_viewset.client import set_celery_dispatch_hook

    celery_app = MagicMock()
    redis_mock = MagicMock()

    received_kwargs = {}

    async def hook(kwargs):
        received_kwargs.update(kwargs)
        return {}

    original_register = result_reader.register_future

    def mock_register(_correlation_id, future, _queue_key=None):
        asyncio.get_event_loop().call_soon(lambda: future.set_result(None))

    result_reader.register_future = mock_register
    set_celery_dispatch_hook(hook)
    try:

        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return []

        instance = ItemViewSet()
        asyncio.run(instance.list_items(fltr="mine"))

        assert received_kwargs == {"fltr": "mine"}
    finally:
        set_celery_dispatch_hook(None)
        result_reader.register_future = original_register


def test_celery_dispatch_hook_empty_dict_adds_nothing():
    """A hook returning {} leaves send_task kwargs unchanged besides the built-in ones."""
    from fastapi_viewsets.decorators.celery_viewset import result_reader
    from fastapi_viewsets.decorators.celery_viewset.client import set_celery_dispatch_hook

    celery_app = MagicMock()
    redis_mock = MagicMock()

    async def hook(_kwargs):
        return {}

    original_register = result_reader.register_future

    def mock_register(_correlation_id, future, _queue_key=None):
        asyncio.get_event_loop().call_soon(lambda: future.set_result(None))

    result_reader.register_future = mock_register
    set_celery_dispatch_hook(hook)
    try:

        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return []

        instance = ItemViewSet()
        asyncio.run(instance.list_items())

        sent_kwargs = celery_app.send_task.call_args.kwargs["kwargs"]
        assert set(sent_kwargs) == {"_correlation_id", "_result_queue_key"}
    finally:
        set_celery_dispatch_hook(None)
        result_reader.register_future = original_register


# ---------------------------------------------------------------------------
# FastAPI integration - client
# ---------------------------------------------------------------------------


def test_fastapi_client_endpoint_returns_200():
    """FastAPI app with celery_viewset_client: calling the patched 'list' method sends a Celery task."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from fastapi_viewsets.decorators.celery_viewset import result_reader

    celery_app = MagicMock()
    redis_mock = MagicMock()

    original_register = result_reader.register_future

    def mock_register(_correlation_id, future, _queue_key=None):
        loop = future.get_loop()
        loop.call_soon_threadsafe(future.set_result, [{"id": 1, "name": "test"}])

    result_reader.register_future = mock_register
    try:

        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return []

        # Build a minimal FastAPI app that calls the patched method directly
        app = FastAPI()

        @app.get("/items/")
        async def list_items(fltr: Any = None):
            instance = ItemViewSet()
            return await instance.list_items(fltr)

        with TestClient(app) as client:
            response = client.get("/items/")
            assert response.status_code == 200
            assert response.json() == [{"id": 1, "name": "test"}]
            celery_app.send_task.assert_called_once()
            assert celery_app.send_task.call_args[0][0] == "items.list_items"
    finally:
        result_reader.register_future = original_register
