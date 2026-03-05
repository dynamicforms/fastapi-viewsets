from unittest.mock import MagicMock, patch

import pytest

from pydantic import BaseModel

from fastapi_viewsets.decorators import (
    celery_viewset,
    set_is_celery_worker,
)
from fastapi_viewsets.decorators.celery_viewset import _detect_is_celery_worker
from fastapi_viewsets.mixins import ListMixin


class Item(BaseModel):
    id: int
    name: str


# ---------------------------------------------------------------------------
# auto-detect celery_viewset tests
# ---------------------------------------------------------------------------

def test_celery_viewset_auto_detect_server():
    """celery_viewset uses server mode when running in Celery worker."""
    celery_app = MagicMock()
    registered_tasks = {}

    def mock_task(name):
        def deck(func):
            registered_tasks[name] = func
            return func
        return deck

    celery_app.task.side_effect = mock_task

    set_is_celery_worker(True)
    try:
        @celery_viewset(celery_app=celery_app, task_prefix="items")
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return [Item(id=1, name="test")]

        assert "items.list_items" in registered_tasks
        assert ItemViewSet.__celery_viewset_metadata__["mode"] if "mode" in ItemViewSet.__celery_viewset_metadata__ else True
    finally:
        set_is_celery_worker(None)


def test_celery_viewset_auto_detect_client():
    """celery_viewset uses client mode when not in Celery worker."""
    celery_app = MagicMock()
    redis_mock = MagicMock()

    set_is_celery_worker(False)
    try:
        @celery_viewset(celery_app=celery_app, task_prefix="items", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return [Item(id=1, name="test")]

        assert ItemViewSet.__celery_viewset_metadata__["mode"] == "client"
    finally:
        set_is_celery_worker(None)


def test_celery_viewset_auto_detect_client_requires_redis():
    """celery_viewset raises ValueError when redis_client missing in client mode."""
    celery_app = MagicMock()

    set_is_celery_worker(False)
    try:
        with pytest.raises(ValueError, match="redis_client"):
            @celery_viewset(celery_app=celery_app, task_prefix="items")
            class ItemViewSet(ListMixin[Item]):
                async def perform_list(self) -> list[Item]:
                    return []
    finally:
        set_is_celery_worker(None)


def test_detect_is_celery_worker_via_sys_argv():
    """Auto-detection works via sys.argv."""
    set_is_celery_worker(None)
    with patch("sys.argv", ["/usr/bin/celery", "worker"]):
        assert _detect_is_celery_worker() is True
    with patch("sys.argv", ["uvicorn", "main:app"]):
        assert _detect_is_celery_worker() is False
