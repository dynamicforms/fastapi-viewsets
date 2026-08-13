"""
The list pipeline's parameters crossing into a Celery worker.

`celery_viewset_client` patches the *route endpoints* - `list_items`, not `perform_list` - so the
whole pipeline runs in the worker: filter, sort and pagination included. These tests pin that,
because it is the property that decides where push-down belongs. A backend that translates part of
the query into its own query does so in the worker, where the records are; nothing has to be
reported back to the FastAPI process, and nothing unreduced has to cross the queue.
"""

from unittest.mock import MagicMock

from pydantic import BaseModel, Field

from fastapi_viewsets.decorators import celery_viewset_server
from fastapi_viewsets.list_query import ListQuery, ListRecords
from fastapi_viewsets.mixins import make_all_optional, PaginatedListMixin


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

    def mock_task(name):
        def decorator(func):
            registered[name] = func
            return func
        return decorator

    celery_app.task.side_effect = mock_task
    viewset_factory(celery_viewset_server(celery_app=celery_app, task_prefix="tracks"))
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
    page = task(context={}, offset=5, limit=3)
    assert [track.id for track in page.results] == [6, 7, 8]
    assert page.has_more is True
    assert page.has_previous is True


def test_the_sort_parameter_reaches_the_worker():
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    page = task(context={}, sort="id:desc", limit=3)
    assert [track.id for track in page.results] == [30, 29, 28]


def test_the_filter_model_survives_the_round_trip_as_a_dict():
    """
    Celery carries the filter as JSON; the worker rebuilds it from the endpoint's type hints. If
    that reconstruction broke, the filter would arrive as a plain dict and `fltr.year` would raise.
    """
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    page = task(context={}, fltr={"year": 2003}, limit=50)
    assert page.results
    assert all(track.year == 2003 for track in page.results)


def test_filter_sort_and_paging_compose_in_the_worker():
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    page = task(context={}, fltr={"year": 2003}, sort="id:desc", offset=1, limit=2)
    assert [track.id for track in page.results] == [23, 18]
    assert page.has_more is True


def test_the_default_page_size_applies_in_the_worker():
    task = worker_tasks_for(paged_viewset)["tracks.list_items"]
    assert len(task(context={}).results) == 10


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
    page = task(context={}, sort="id:asc", limit=3)
    assert [track.id for track in page.results] == [30, 29, 28]
