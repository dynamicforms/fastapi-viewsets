import asyncio

from datetime import date, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from pydantic import BaseModel

from fastapi_viewsets.decorators import (
    celery_viewset_client,
)
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

    def mock_register(_correlation_id, future):
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
        result = asyncio.get_event_loop().run_until_complete(instance.list_items())
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

    def mock_register(_correlation_id, future):
        asyncio.get_event_loop().call_soon(lambda: future.set_result(None))

    result_reader.register_future = mock_register
    try:

        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(CreateMixin[int, ItemWithDate]):
            async def perform_create(self, data: ItemWithDate) -> ItemWithDate:
                return data

        instance = ItemViewSet()
        payload = ItemWithDate(id=1, created=date(2026, 8, 1), updated=datetime(2026, 8, 1, 12, 30))
        asyncio.get_event_loop().run_until_complete(instance.create(data=payload))

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

    def mock_register(_correlation_id, future):
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
        asyncio.get_event_loop().run_until_complete(instance.create(payload))

        sent_args = celery_app.send_task.call_args.kwargs["args"]
        assert sent_args == [{"id": 1, "created": "2026-08-01", "updated": "2026-08-01T12:30:00"}]
        # Must not raise - this is what Kombu's JSON encoder does under the hood.
        json.dumps(sent_args)
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

    asyncio.get_event_loop().run_until_complete(run())


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

    def mock_register(_correlation_id, future):
        asyncio.get_event_loop().call_soon(lambda: future.set_result(["mocked"]))

    result_reader.register_future = mock_register
    try:

        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return [Item(id=1, name="test")]

        instance = ItemViewSet()
        asyncio.get_event_loop().run_until_complete(instance.list_items())

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
    celery_app.send_task.side_effect = lambda *_args, **_kwargs: call_order.append("send_task")

    async def hook():
        call_order.append("hook")
        return {"_extra_field": "abc"}

    original_register = result_reader.register_future

    def mock_register(_correlation_id, future):
        asyncio.get_event_loop().call_soon(lambda: future.set_result(None))

    result_reader.register_future = mock_register
    set_celery_dispatch_hook(hook)
    try:

        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return []

        instance = ItemViewSet()
        asyncio.get_event_loop().run_until_complete(instance.list_items())

        assert call_order == ["hook", "send_task"]
        sent_kwargs = celery_app.send_task.call_args.kwargs["kwargs"]
        assert sent_kwargs["_extra_field"] == "abc"
    finally:
        set_celery_dispatch_hook(None)
        result_reader.register_future = original_register


def test_celery_dispatch_hook_empty_dict_adds_nothing():
    """A hook returning {} leaves send_task kwargs unchanged besides the built-in ones."""
    from fastapi_viewsets.decorators.celery_viewset import result_reader
    from fastapi_viewsets.decorators.celery_viewset.client import set_celery_dispatch_hook

    celery_app = MagicMock()
    redis_mock = MagicMock()

    async def hook():
        return {}

    original_register = result_reader.register_future

    def mock_register(_correlation_id, future):
        asyncio.get_event_loop().call_soon(lambda: future.set_result(None))

    result_reader.register_future = mock_register
    set_celery_dispatch_hook(hook)
    try:

        @celery_viewset_client(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return []

        instance = ItemViewSet()
        asyncio.get_event_loop().run_until_complete(instance.list_items())

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

    def mock_register(_correlation_id, future):
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
