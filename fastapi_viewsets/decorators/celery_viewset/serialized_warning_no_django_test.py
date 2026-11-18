"""The `DjangoORMViewSet` check of an async Celery process neither needs nor imports the Django ORM backend."""

import asyncio
import logging
import sys

from unittest.mock import MagicMock

import pytest

from fastapi import APIRouter

from . import server
from .server import celery_viewset_server

LOGGER = server.logger.name


class Plain:
    __router = APIRouter()

    @__router.get("/ping")
    async def ping(self) -> str:
        return "pong"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(server, "_serialized_warned", set())


def test_a_viewset_in_an_async_process_without_the_django_orm_backend(monkeypatch, caplog):
    monkeypatch.delitem(sys.modules, server._DJANGO_ORM_MODULE, raising=False)
    tasks = {}
    celery_app = MagicMock()
    celery_app.task.side_effect = lambda name, **_options: lambda fun: tasks.setdefault(name, fun)
    celery_viewset_server(celery_app, "plain_no_django", redis_client=MagicMock())(Plain)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert asyncio.run(tasks["plain_no_django.ping"].__celery_async__()) == "pong"

    assert [r for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING] == []
    assert server._DJANGO_ORM_MODULE not in sys.modules
