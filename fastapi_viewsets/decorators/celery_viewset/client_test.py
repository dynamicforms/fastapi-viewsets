import asyncio

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
