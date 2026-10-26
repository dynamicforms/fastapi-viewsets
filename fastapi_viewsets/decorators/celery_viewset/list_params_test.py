"""
The list pipeline's parameters crossing into a Celery worker.

`celery_viewset_client` patches the *route endpoints* - `list_items`, not `perform_list` - so the
whole pipeline runs in the worker: filter, sort and pagination included. These tests pin that,
because it is the property that decides where push-down belongs. A backend that translates part of
the query into its own query does so in the worker, where the records are; nothing has to be
reported back to the FastAPI process, and nothing unreduced has to cross the queue.
"""

from typing import Annotated
from unittest.mock import MagicMock

import pytest

from pydantic import BaseModel, Field

from fastapi_viewsets.decorators import celery_viewset_client, celery_viewset_server
from fastapi_viewsets.list_query import ListQuery, ListRecords
from fastapi_viewsets.mixins import make_all_optional, MultiTypeListMixin, PaginatedListMixin


class Track(BaseModel):
    id: int = Field(json_schema_extra={"autoinc_int": True})
    title: str
    year: int


TrackFilter = make_all_optional(Track)

LIBRARY = [Track(id=n, title=f"Track {n:03d}", year=2000 + (n % 5)) for n in range(1, 31)]


def worker_tasks_for(viewset_factory) -> dict:
    """Registers a viewset against a mock Celery app and hands back its task functions."""
    celery_app = MagicMock()
    registered: dict = {}

    def mock_task(name, **_kwargs):
        def decorator(func):
            registered[name] = func
            return func

        return decorator

    celery_app.task.side_effect = mock_task
    viewset_factory(celery_viewset_server(celery_app=celery_app, task_prefix="tracks", redis_client=MagicMock()))
    return registered


def paged_viewset(decorate):
    @decorate
    class TrackViewSet(PaginatedListMixin[Track, TrackFilter]):
        default_page_size = 10

        async def perform_list(self, _context) -> list[Track]:
            return list(LIBRARY)

        async def filter_list(self, fltr, records):
            return [track for track in records if fltr.year is None or track.year == fltr.year]

    return TrackViewSet


def test_pagination_parameters_reach_the_worker():
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    page = task(offset=5, limit=3)
    assert [track.id for track in page.results] == [6, 7, 8]
    assert page.has_more is True
    assert page.has_previous is True


def test_the_sort_parameter_reaches_the_worker():
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    page = task(sort="id:desc", limit=3)
    assert [track.id for track in page.results] == [30, 29, 28]


def test_the_filter_model_survives_the_round_trip_as_a_dict():
    """
    Celery carries the filter as JSON; the worker rebuilds it from the endpoint's type hints. If
    that reconstruction broke, the filter would arrive as a plain dict and `fltr.year` would raise.
    """
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    page = task(fltr={"year": 2003}, limit=50)
    assert page.results
    assert all(track.year == 2003 for track in page.results)


def test_filter_sort_and_paging_compose_in_the_worker():
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    page = task(fltr={"year": 2003}, sort="id:desc", offset=1, limit=2)
    assert [track.id for track in page.results] == [23, 18]
    assert page.has_more is True


def test_the_default_page_size_applies_in_the_worker():
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    assert len(task().results) == 10


def test_a_stage_the_backend_handled_is_not_redone_in_the_worker():
    """
    mark_applied() is an intra-process contract, and the worker is that process. A backend that
    sorted in its own query says so and the in-memory sort leaves the rows alone - no round trip
    back to the FastAPI side, which would mean shipping unsorted records across the queue only to
    sort them somewhere else.
    """

    def pushdown_viewset(decorate):
        @decorate
        class PushdownViewSet(PaginatedListMixin[Track, TrackFilter]):
            async def perform_list(self, _context) -> list[Track]:
                return list(reversed(LIBRARY))

            async def apply_sort(self, context, query: ListQuery, records: ListRecords) -> ListRecords:
                query.mark_applied("sort")
                return await super().apply_sort(context, query, records)

        return PushdownViewSet

    task = worker_tasks_for(pushdown_viewset)["tracks.list_items"]
    page = task(sort="id:asc", limit=3)
    assert [track.id for track in page.results] == [30, 29, 28]


class TrackTitle(BaseModel):
    title: str


def typed_viewset(decorate):
    @decorate
    class TypedTrackViewSet(PaginatedListMixin[Track, TrackFilter], MultiTypeListMixin[Annotated[TrackTitle, "title"]]):
        default_page_size = 10

        async def perform_list(self, _context) -> list[Track]:
            return list(LIBRARY)

    return TypedTrackViewSet


def test_the_list_type_reaches_the_worker():
    task = worker_tasks_for(typed_viewset)["tracks.list_items"]
    page = task(x_list_type="title", sort="year:desc,id:asc", limit=2)
    assert page.results == [TrackTitle(title="Track 004"), TrackTitle(title="Track 009")]
    assert type(task(limit=1).results[0]) is Track


def test_a_request_header_selects_the_type_the_worker_answers_in(monkeypatch):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from ..route_viewset import route_viewset
    from .error_round_trip_test import _Executor

    executor = _Executor(monkeypatch)
    typed_viewset(
        celery_viewset_server(celery_app=executor.server_app, task_prefix="tracks", redis_client=executor.redis)
    )
    client_cls = typed_viewset(
        celery_viewset_client(celery_app=executor.client_app, task_prefix="tracks", redis_client=MagicMock())
    )
    router = APIRouter()
    route_viewset(router, "/tracks")(client_cls)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/tracks", params={"limit": 2}, headers={"X-List-Type": "title"})
    assert response.json()["results"] == [{"title": "Track 001"}, {"title": "Track 002"}]
    assert executor.client_app.send_task.call_args.kwargs["kwargs"]["x_list_type"] == "title"
    assert set(client.get("/tracks", params={"limit": 1}).json()["results"][0]) == {"id", "title", "year"}


class TrackNote(BaseModel):
    id: int
    note: str = "none"


class TrackId(BaseModel):
    id: int


@pytest.mark.parametrize("shape", ["plain", "paginated", "cursor"])
def test_the_worker_answer_validates_as_the_selected_type_when_another_type_accepts_it(monkeypatch, shape):
    """`{"id": 1}` validates as `TrackNote` too; the response is still `TrackId`'s, without `note`."""
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from ..route_viewset import route_viewset
    from .error_round_trip_test import _Executor

    def overlapping_viewset(decorate):
        @decorate
        class OverlappingTrackViewSet(PaginatedListMixin[Track, TrackFilter], MultiTypeListMixin[TrackNote, TrackId]):
            list_shape = shape

            async def perform_list(self, _context) -> list[Track]:
                return LIBRARY[:2]

        return OverlappingTrackViewSet

    executor = _Executor(monkeypatch)
    overlapping_viewset(
        celery_viewset_server(celery_app=executor.server_app, task_prefix="tracks", redis_client=executor.redis)
    )
    client_cls = overlapping_viewset(
        celery_viewset_client(celery_app=executor.client_app, task_prefix="tracks", redis_client=MagicMock())
    )
    router = APIRouter()
    route_viewset(router, "/tracks")(client_cls)
    app = FastAPI()
    app.include_router(router)

    body = TestClient(app).get("/tracks", headers={"X-List-Type": "TrackId"}).json()
    rows = body if shape == "plain" else body["results"]
    assert rows == [{"id": 1}, {"id": 2}]
