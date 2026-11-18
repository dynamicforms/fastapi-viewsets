"""A `DjangoORMViewSet` executed in an async process of Celery with async tasks logs, once per process and
viewset class, that its queries run one at a time on one thread."""

import asyncio
import logging

from unittest.mock import MagicMock

import pytest

from fastapi import APIRouter
from pydantic import BaseModel

pytest.importorskip("django")

from django.db import models  # noqa: E402 - after the skip

from ...backends.django_orm import DjangoORMViewSet  # noqa: E402
from . import server  # noqa: E402
from .server import celery_viewset_server  # noqa: E402

LOGGER = server.logger.name


class SerializedNote(models.Model):
    text = models.CharField(max_length=20)

    class Meta:
        app_label = "backends"


class Note(BaseModel):
    id: int
    text: str


class NoteViewSet(DjangoORMViewSet[int, Note]):
    model = SerializedNote
    schema = Note
    __router = APIRouter()

    @__router.get("/ping")
    async def ping(self) -> str:
        return "pong"


class Plain:
    __router = APIRouter()

    @__router.get("/ping")
    async def ping(self) -> str:
        return "pong"


def task(cls, prefix: str):
    tasks = {}
    celery_app = MagicMock()
    celery_app.task.side_effect = lambda name, **_options: lambda fun: tasks.setdefault(name, fun)
    celery_viewset_server(celery_app, prefix, redis_client=MagicMock())(cls)
    return tasks[f"{prefix}.ping"]


def warnings_logged(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == LOGGER and r.levelno == logging.WARNING]


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(server, "_serialized_warned", set())


def test_a_django_orm_viewset_in_an_async_process_warns_once(caplog):
    run = task(NoteViewSet, "notes").__celery_async__
    with caplog.at_level(logging.WARNING, logger=LOGGER):

        async def scenario():
            return await asyncio.gather(*(run() for _ in range(3)))

        assert asyncio.run(scenario()) == ["pong"] * 3
        assert asyncio.run(run()) == "pong"
    [message] = warnings_logged(caplog)
    assert "NoteViewSet" in message
    assert "one at a time on one thread" in message


def test_each_django_orm_viewset_class_warns(caplog):
    class OtherNoteViewSet(NoteViewSet):
        pass

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        asyncio.run(task(NoteViewSet, "notes_a").__celery_async__())
        asyncio.run(task(OtherNoteViewSet, "notes_b").__celery_async__())
    assert len(warnings_logged(caplog)) == 2


def test_another_viewset_in_an_async_process_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert asyncio.run(task(Plain, "plain").__celery_async__()) == "pong"
    assert warnings_logged(caplog) == []


def test_a_django_orm_viewset_run_by_the_sync_function_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert task(NoteViewSet, "notes_sync")() == "pong"
    assert warnings_logged(caplog) == []
