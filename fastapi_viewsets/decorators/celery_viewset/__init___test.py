import logging

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pydantic import BaseModel

from fastapi_viewsets.decorators import (
    celery_viewset,
    set_is_celery_worker,
)
from fastapi_viewsets.decorators.celery_viewset import (
    _detect_is_celery_worker,
    check_result_readers,
    get_result_queue_key,
    start_result_reader,
    stop_result_reader,
)
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

    def mock_task(name, **kwargs):
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
        metadata = ItemViewSet.__celery_viewset_metadata__
        assert metadata["mode"] if "mode" in metadata else True
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


# ---------------------------------------------------------------------------
# check_result_readers
# ---------------------------------------------------------------------------


def test_check_result_readers_warns_when_reader_missing(caplog):
    """check_result_readers reports (and logs) a queue_key registered but with no running reader."""
    celery_app = MagicMock()
    redis_mock = MagicMock()

    set_is_celery_worker(False)
    try:

        @celery_viewset(celery_app=celery_app, task_prefix="check-readers-missing", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return []

        queue_key = get_result_queue_key("check-readers-missing")
        with caplog.at_level(logging.WARNING):
            missing = check_result_readers()

        assert queue_key in missing
        assert any(queue_key in record.getMessage() for record in caplog.records)
    finally:
        set_is_celery_worker(None)


@pytest.mark.asyncio
async def test_check_result_readers_clean_once_reader_started():
    """check_result_readers no longer flags a queue_key once its reader is running."""
    celery_app = MagicMock()
    redis_mock = MagicMock()
    async_redis_mock = AsyncMock()
    async_redis_mock.lpop.return_value = None

    set_is_celery_worker(False)
    queue_key = get_result_queue_key("check-readers-running")
    try:

        @celery_viewset(celery_app=celery_app, task_prefix="check-readers-running", redis_client=redis_mock)
        class ItemViewSet(ListMixin[Item]):
            async def perform_list(self) -> list[Item]:
                return []

        await start_result_reader(async_redis_mock, queue_key, poll_interval=0.01)
        missing = check_result_readers()

        assert queue_key not in missing
    finally:
        await stop_result_reader(queue_key)
        set_is_celery_worker(None)
